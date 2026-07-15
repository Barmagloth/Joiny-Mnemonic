from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic import hooks as hook_module
from joiny_mnemonic.hooks import install_hooks, process_hook
from joiny_mnemonic.evaluation import (
    EvaluationTask,
    FullHistoryPolicy,
    ResumePolicy,
    TaskRun,
    assert_task_quality,
    evaluate_policies,
    evaluate_with_runner,
)
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.transcript import interaction_groups


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class ConsolidationAndHooksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)

    def tearDown(self) -> None:
        self.service.close()

    def test_explicit_markers_create_sourced_memory_and_protected_blocks(self) -> None:
        event = self.service.store.append_host_event(
            adapter="claude",
            kind="message",
            role="user",
            content=(
                "Goal: ship the durable core\n"
                "Decision: use SQLite\n"
                "TODO: add hooks\n"
                "Fact: WAL is enabled\n"
                "Constraint: operate offline\n"
                "Preference: keep reports concise"
            ),
        )
        result = self.service.consolidator.consolidate_event(self.service, event)
        self.assertEqual(len(result.memory_ids), 6)
        blocks = self.service.store.get_active_blocks()
        self.assertEqual(blocks["goal"].content, "ship the durable core")
        self.assertIn("use SQLite", blocks["decisions"].content)
        self.assertIn("add hooks", blocks["open_tasks"].content)
        self.assertIn("operate offline", blocks["constraints"].content)
        memory_types = {
            self.service.store.get_memory(memory_id).memory_type
            for memory_id in result.memory_ids
        }
        self.assertTrue({"fact", "decision", "task", "preference"}.issubset(memory_types))
        for memory_id in result.memory_ids:
            self.assertIn(event.id, self.service.store.get_memory(memory_id).source_event_ids)
        repeated = self.service.consolidator.consolidate_event(self.service, event)
        self.assertEqual(repeated.memory_ids, result.memory_ids)

    def test_unmarked_fact_is_searchable_but_not_automatically_resumed(self) -> None:
        source = self.service.store.append_event(
            kind="message",
            role="user",
            content="The verified build codename is ORBITAL741.",
        )
        unmarked = self.service.consolidator.consolidate_event(self.service, source)
        self.assertEqual(unmarked.memory_ids, ())
        for index in range(120):
            self.service.store.append_event(
                kind="message", role="user", content=f"routine distractor {index:04d}"
            )

        task = EvaluationTask(
            id="unmarked-boundary",
            query="verified build codename",
            required_evidence=("ORBITAL741",),
        )
        report = evaluate_policies(
            self.service,
            [task],
            policies=[FullHistoryPolicy(), ResumePolicy(768)],
        )
        rows = {row["policy"]: row for row in report["results"]}
        self.assertEqual(rows["full-history"]["quality"], 1.0)
        self.assertEqual(rows["resume-768"]["quality_vs_full_history"], 0.0)
        hits = self.service.search(query="ORBITAL741", include_events=True, semantic=False)
        self.assertIn(source.id, {hit.id for hit in hits})

        marked_source = self.service.store.append_event(
            kind="message", role="assistant", content="Fact: Build codename is ORBITAL741."
        )
        marked = self.service.consolidator.consolidate_event(self.service, marked_source)
        self.assertEqual(len(marked.memory_ids), 1)
        for index in range(120, 240):
            self.service.store.append_event(
                kind="message", role="user", content=f"routine distractor {index:04d}"
            )
        promoted = evaluate_policies(
            self.service,
            [task],
            policies=[FullHistoryPolicy(), ResumePolicy(768)],
        )
        promoted_rows = {row["policy"]: row for row in promoted["results"]}
        self.assertEqual(promoted_rows["resume-768"]["quality_vs_full_history"], 1.0)

    def test_compaction_is_extractive_and_provenance_bound(self) -> None:
        events = [
            self.service.store.append_event(kind="message", role="user", content=f"message {index}")
            for index in range(5)
        ]
        result = self.service.compact(keep_recent_groups=1, summary_budget=300)
        self.assertIsNotNone(result.summary)
        self.assertEqual(result.source_event_ids, tuple(event.id for event in events[:-1]))
        for event in events[:-1]:
            self.assertIn(f"[{event.id}]", result.text)
        self.assertEqual(
            [event.id for event in self.service.exact_source(result.summary.id)],
            list(result.source_event_ids),
        )

    def test_hook_retry_is_idempotent_and_tool_pair_is_atomic(self) -> None:
        value = {
            "hook_event_name": "PostToolUse",
            "session_id": "native-1",
            "tool_use_id": "call-1",
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
            "tool_response": {"content": "ok"},
        }
        self.assertEqual(process_hook(self.service, "codex", value), {})
        self.assertEqual(process_hook(self.service, "codex", value), {})
        events = self.service.store.query_events(kinds=("tool_call", "tool_output"))
        self.assertEqual([event.kind for event in events], ["tool_call", "tool_output"])
        self.assertTrue(all(event.origin_channel == "host_hook" for event in events))
        self.assertTrue(all(event.origin_adapter == "codex" for event in events))
        groups = interaction_groups(events)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_session_hook_injects_resume_context(self) -> None:
        output = process_hook(
            self.service,
            "codex",
            {"hook_event_name": "SessionStart", "session_id": "native-2", "source": "startup"},
        )
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[MEMORY PACKET]", context)
        self.assertIn("[DURABLE MEMORY CAPTURE]", context)
        self.assertIn("Fact:", context)
        self.assertIn("Unmarked prose remains searchable", context)

    def test_agent_marker_in_stop_hook_is_promoted(self) -> None:
        output = process_hook(
            self.service,
            "codex",
            {
                "hook_event_name": "Stop",
                "session_id": "native-durable-marker",
                "last_assistant_message": "Fact: Deployment requires the X-Trace header.",
            },
        )
        self.assertEqual(output, {})
        memories = self.service.store.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].memory_type, "fact")
        self.assertEqual(memories[0].content, "Deployment requires the X-Trace header.")
        self.assertEqual(
            self.service.exact_source(memories[0].id)[0].content,
            "Fact: Deployment requires the X-Trace header.",
        )

    def test_installers_write_real_project_configs_and_preserve_existing_hooks(self) -> None:
        root = RUNTIME_ROOT / f"install-{uuid.uuid4().hex}"
        (root / ".claude").mkdir(parents=True)
        existing = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "existing"}]},
                    {"hooks": [{"type": "command", "command": "python -m llm_memory hook --agent claude-code"}]},
                ]
            }
        }
        claude_settings = root / ".claude" / "settings.json"
        original_claude = json.dumps(existing).encode("utf-8")
        claude_settings.write_bytes(original_claude)
        legacy_plugin = root / ".opencode" / "plugins" / "llm-memory.js"
        legacy_plugin.parent.mkdir(parents=True)
        legacy_plugin.write_text("export const LlmMemoryPlugin = () => {}; // -m llm_memory", encoding="utf-8")

        claude_install = install_hooks("claude-code", root)
        install_hooks("codex", root)
        install_hooks("openhands", root)
        install_hooks("opencode", root)

        self.assertIsNotNone(claude_install.backup_file)
        backup = Path(claude_install.backup_file)
        self.assertEqual(backup.read_bytes(), original_claude)
        claude = json.loads(claude_settings.read_text(encoding="utf-8"))
        self.assertEqual(claude["hooks"]["Stop"][0]["hooks"][0]["command"], "existing")
        self.assertNotIn("llm_memory", json.dumps(claude))
        # Claude Code rejects the entire settings file over one unknown hook
        # event key; PostCompact must never be written for claude-code and a
        # reinstall must purge our own stale PostCompact entries while
        # preserving foreign ones under the same key.
        self.assertNotIn("PostCompact", claude["hooks"])
        claude["hooks"]["PostCompact"] = [
            {"hooks": [
                {"type": "command", "command": "python -m joiny_mnemonic --db x hook --agent claude-code"},
                {"type": "command", "command": "foreign-postcompact"},
            ]}
        ]
        claude_settings.write_text(json.dumps(claude), encoding="utf-8")
        install_hooks("claude-code", root)
        purged = json.loads(claude_settings.read_text(encoding="utf-8"))
        remaining = [
            hook["command"]
            for entry in purged["hooks"].get("PostCompact", [])
            for hook in entry.get("hooks", [])
        ]
        self.assertEqual(remaining, ["foreign-postcompact"])
        codex_after = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("PostCompact", codex_after["hooks"])
        codex = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("UserPromptSubmit", codex["hooks"])
        # Regression (first live run): hosts execute hook commands through a
        # POSIX shell even on Windows, where unquoted backslashes are eaten as
        # escapes and every hook died with exit 127. Installed commands must
        # contain no backslashes on any platform.
        installed = claude["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertNotIn("\\", installed)
        self.assertIn("-m joiny_mnemonic", installed)

        # Regression (first live run, part two): a reinstall whose command
        # line changed must REPLACE the stale own entry, not append a second
        # delivery next to a dead one. Simulate an old-format leftover.
        stale = claude
        stale["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] = (
            "C:\\old\\python.exe -m joiny_mnemonic --db old.db hook --agent claude-code"
        )
        claude_settings.write_text(json.dumps(stale), encoding="utf-8")
        install_hooks("claude-code", root)
        refreshed = json.loads(claude_settings.read_text(encoding="utf-8"))
        own = [
            hook["command"]
            for entry in refreshed["hooks"]["UserPromptSubmit"]
            for hook in entry.get("hooks", [])
            if "-m joiny_mnemonic" in hook.get("command", "")
        ]
        self.assertEqual(len(own), 1, own)
        self.assertNotIn("\\", own[0])
        self.assertEqual(
            refreshed["hooks"]["Stop"][0]["hooks"][0]["command"], "existing"
        )
        for section in codex["hooks"].values():
            for entry in section:
                for hook in entry.get("hooks", []):
                    self.assertNotIn("\\", hook.get("command", ""))
        openhands = json.loads((root / ".openhands" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("post_tool_use", openhands)
        plugin = (root / ".opencode" / "plugins" / "joiny-mnemonic.js").read_text(encoding="utf-8")
        self.assertIn("experimental.session.compacting", plugin)
        self.assertIn("experimental.chat.system.transform", plugin)
        self.assertIn("intentionally inert", legacy_plugin.read_text(encoding="utf-8"))
        self.assertNotIn("LlmMemoryPlugin", legacy_plugin.read_text(encoding="utf-8"))

    def test_invalid_claude_json_is_rejected_without_partial_install(self) -> None:
        root = RUNTIME_ROOT / f"invalid-claude-{uuid.uuid4().hex}"
        settings = root / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        invalid = b'{"hooks": {"Stop": []} "permissions": {}}'
        settings.write_bytes(invalid)

        with self.assertRaisesRegex(
            ValueError, r"invalid JSON at line 1, column .*not modified"
        ):
            install_hooks("claude-code", root)

        self.assertEqual(settings.read_bytes(), invalid)
        self.assertFalse(
            settings.with_suffix(".json.joiny-mnemonic.bak").exists()
        )
        self.assertFalse((root / ".joiny-mnemonic" / "context-limits.json").exists())
    def test_failed_claude_write_restores_verified_backup(self) -> None:
        root = RUNTIME_ROOT / f"rollback-claude-{uuid.uuid4().hex}"
        settings = root / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        original = b'{"permissions": {"allow": ["Read"]}}'
        settings.write_bytes(original)
        real_write = hook_module._durable_write
        failed = False

        def fail_target_once(path: Path, data: bytes) -> None:
            nonlocal failed
            if path == settings and not failed:
                failed = True
                path.write_bytes(b'{"partial":')
                raise OSError("simulated interrupted settings write")
            real_write(path, data)

        with (
            patch.object(Path, "replace", side_effect=PermissionError),
            patch.object(hook_module, "_durable_write", side_effect=fail_target_once),
            self.assertRaisesRegex(OSError, "simulated interrupted"),
        ):
            install_hooks("claude-code", root)

        self.assertTrue(failed)
        self.assertEqual(settings.read_bytes(), original)
        backup = settings.with_suffix(".json.joiny-mnemonic.bak")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["permissions"], {
            "allow": ["Read"]
        })
    def test_task_runner_evaluation_is_distinct_from_evidence_diagnostic(self) -> None:
        event = self.service.store.append_event(
            kind="message", role="user", content="Decision: use SQLite"
        )
        self.service.consolidator.consolidate_event(self.service, event)
        task = EvaluationTask(id="decision", query="SQLite", task_input="Which database?")

        class Runner:
            name = "test-task-runner"

            def run(self, selected: EvaluationTask, context: object) -> TaskRun:
                text = getattr(context, "text")
                success = "SQLite" in text
                return TaskRun(success, "SQLite" if success else "unknown", float(success))

        report = evaluate_with_runner(
            self.service,
            [task],
            Runner(),
            policies=[FullHistoryPolicy(), ResumePolicy(1500)],
        )
        self.assertTrue(report["task_level"])
        assert_task_quality(report, 0.95)
        diagnostic = evaluate_policies(self.service, [task])
        self.assertFalse(diagnostic["task_level"])
        with self.assertRaises(AssertionError):
            assert_task_quality(diagnostic, 0.95)


if __name__ == "__main__":
    unittest.main()
