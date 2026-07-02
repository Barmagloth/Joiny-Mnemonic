from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

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
        event = self.service.store.append_event(
            kind="message",
            role="user",
            content="Goal: ship the durable core\nDecision: use SQLite\nTODO: add hooks",
        )
        result = self.service.consolidator.consolidate_event(self.service, event)
        self.assertEqual(len(result.memory_ids), 3)
        blocks = self.service.store.get_active_blocks()
        self.assertEqual(blocks["goal"].content, "ship the durable core")
        self.assertIn("use SQLite", blocks["decisions"].content)
        self.assertIn("add hooks", blocks["open_tasks"].content)
        for memory_id in result.memory_ids:
            self.assertIn(event.id, self.service.store.get_memory(memory_id).source_event_ids)
        repeated = self.service.consolidator.consolidate_event(self.service, event)
        self.assertEqual(repeated.memory_ids, result.memory_ids)

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
        (root / ".claude" / "settings.json").write_text(json.dumps(existing), encoding="utf-8")
        legacy_plugin = root / ".opencode" / "plugins" / "llm-memory.js"
        legacy_plugin.parent.mkdir(parents=True)
        legacy_plugin.write_text("export const LlmMemoryPlugin = () => {}; // -m llm_memory", encoding="utf-8")

        install_hooks("claude-code", root)
        install_hooks("codex", root)
        install_hooks("openhands", root)
        install_hooks("opencode", root)

        claude = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(claude["hooks"]["Stop"][0]["hooks"][0]["command"], "existing")
        self.assertNotIn("llm_memory", json.dumps(claude))
        self.assertIn("PostCompact", claude["hooks"])
        codex = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("UserPromptSubmit", codex["hooks"])
        openhands = json.loads((root / ".openhands" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("post_tool_use", openhands)
        plugin = (root / ".opencode" / "plugins" / "joiny-mnemonic.js").read_text(encoding="utf-8")
        self.assertIn("experimental.session.compacting", plugin)
        self.assertIn("experimental.chat.system.transform", plugin)
        self.assertIn("intentionally inert", legacy_plugin.read_text(encoding="utf-8"))
        self.assertNotIn("LlmMemoryPlugin", legacy_plugin.read_text(encoding="utf-8"))

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
