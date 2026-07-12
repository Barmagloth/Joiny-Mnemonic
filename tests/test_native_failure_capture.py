from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.adapters import adapter_capabilities
from joiny_mnemonic.hooks import install_hooks, process_hook
from joiny_mnemonic.plugins import PluginRegistry
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"


class RejectingPlugin:
    name = "reject-failure"

    def index(self, record: object) -> None:
        raise RuntimeError("semantic unavailable")

    def project(self, record: object) -> None:
        raise RuntimeError("graph unavailable")


class NativeFailureCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)

    def tearDown(self) -> None:
        self.service.close()

    def test_failure_pair_record_provenance_files_and_retry_are_deterministic(self) -> None:
        value = {
            "hook_event_name": "PostToolUseFailure",
            "session_id": "native-failure-1",
            "tool_use_id": "call-failure-1",
            "tool_name": "Write",
            "tool_input": {
                "file_path": "src/auth.py",
                "paths": ["tests/test_auth.py", "src/auth.py"],
            },
            "tool_response": {
                "error": "Permission denied\nignored traceback line",
                "content": "ERROR fallback must not win",
            },
        }

        self.assertEqual(process_hook(self.service, "claude-code", value), {})
        self.assertEqual(process_hook(self.service, "claude-code", value), {})

        pair = self.service.store.query_events(kinds=("tool_call", "tool_output"))
        self.assertEqual([event.kind for event in pair], ["tool_call", "tool_output"])
        self.assertEqual(pair[0].files, ("src/auth.py", "tests/test_auth.py"))
        self.assertEqual(pair[1].files, pair[0].files)
        failures = [
            record
            for record in self.service.store.list_memories(include_superseded=True)
            if record.memory_type == "failure"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].content, "Write failed: Permission denied")
        self.assertEqual(failures[0].source_event_ids, tuple(event.id for event in pair))
        self.assertEqual(failures[0].files, pair[0].files)
        self.assertEqual(self.service.store.get_active_blocks(), {})

    def test_reducer_line_then_first_output_then_generic_fallback(self) -> None:
        cases = (
            ("Shell", "noise\nERROR build failed\nlast", "Shell failed: ERROR build failed"),
            ("Read", "first useful line\nsecond", "Read failed: first useful line"),
            ("Delete", "", "Delete failed"),
        )
        for index, (tool, output, expected) in enumerate(cases):
            process_hook(
                self.service,
                "claude-code",
                {
                    "hook_event_name": "PostToolUseFailure",
                    "session_id": f"native-failure-{index + 2}",
                    "tool_use_id": f"call-failure-{index + 2}",
                    "tool_name": tool,
                    "tool_input": {},
                    "tool_response": output,
                },
            )
            failure = self.service.store.list_memories()[-1]
            self.assertEqual(failure.content, expected)

    def test_optional_plugin_failure_does_not_roll_back_pair_or_record(self) -> None:
        plugins = PluginRegistry(load_installed=False)
        rejecting = RejectingPlugin()
        plugins.register_semantic(rejecting)
        plugins.register_knowledge_graph(rejecting)
        service = MemoryService(":memory:", project_root=RUNTIME_ROOT, plugins=plugins)
        try:
            process_hook(
                service,
                "claude-code",
                {
                    "hook_event_name": "PostToolUseFailure",
                    "session_id": "native-failure-plugin",
                    "tool_use_id": "call-failure-plugin",
                    "tool_name": "Build",
                    "tool_input": {"path": "src/build.py"},
                    "error": "compiler exited 1",
                },
            )
            pair = service.store.query_events(kinds=("tool_call", "tool_output"))
            failures = service.store.list_memories(memory_types=("failure",))
            self.assertEqual(len(pair), 2)
            self.assertEqual(len(failures), 1)
            self.assertEqual(
                failures[0].source_event_ids,
                tuple(event.id for event in pair),
            )
            self.assertEqual(len(service.plugin_errors), 2)
        finally:
            service.close()

    def test_capability_and_installer_report_only_supported_host(self) -> None:
        self.assertTrue(adapter_capabilities("claude-code")["tool_failure_capture"])
        self.assertFalse(adapter_capabilities("codex")["tool_failure_capture"])
        self.assertFalse(adapter_capabilities("opencode")["tool_failure_capture"])
        self.assertFalse(adapter_capabilities("openhands")["tool_failure_capture"])

        root = RUNTIME_ROOT / f"failure-install-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        install_hooks("claude-code", root)
        install_hooks("codex", root)
        claude = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        codex = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("PostToolUseFailure", claude["hooks"])
        self.assertNotIn("PostToolUseFailure", codex["hooks"])


if __name__ == "__main__":
    unittest.main()