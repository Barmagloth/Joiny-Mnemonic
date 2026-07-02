from __future__ import annotations

import sqlite3
import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.cli import build_parser
from joiny_mnemonic.hooks import (
    install_hooks, process_hook, resolve_global_install_path, resolve_hook_project,
)
from joiny_mnemonic.paths import resolve_project_database
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class ReductionUsageGovernorTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)

    def tearDown(self) -> None:
        self.service.close()

    @staticmethod
    def _verbose_failure_output() -> str:
        passed = [f"tests/test_bulk.py::test_case_{index:04d} PASSED" for index in range(600)]
        failure = [
            "=================================== FAILURES ===================================",
            "____________________________ test_accounting ____________________________",
            "tests/test_billing.py:417: in test_accounting",
            "    assert actual_total == expected_total",
            "E   AssertionError: expected 941.25, got 914.25",
            "================ 1 failed, 600 passed, 2 warnings in 19.42s ================",
        ]
        return "\n".join([*passed, *failure])

    def test_reducer_preserves_raw_source_and_critical_failure_signals(self) -> None:
        raw = self._verbose_failure_output()
        event = self.service.store.append_event(
            kind="tool_output",
            role="tool",
            content=raw,
            payload={
                "_memory_call_id": "call-reduce",
                "tool_input": {"command": "pytest -vv"},
            },
        )
        bundle, views = self.service.reduce_tool_output(event)
        compact = next(view for view in views if view.level == "compact")
        self.assertLess(compact.view_tokens, bundle.raw_tokens)
        self.assertEqual(bundle.compact_critical_recall, 1.0)
        self.assertIn("expected 941.25, got 914.25", compact.content)
        self.assertEqual(self.service.store.get_event(event.id).content, raw)
        self.assertEqual(self.service.exact_source(compact.id)[0].content, raw)
        self.assertEqual(self.service.store.verify_chain(), (True, None))
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.store._conn.execute(
                "UPDATE tool_output_views SET content='tampered' WHERE id=?", (compact.id,)
            )

    def test_prompt_uses_compact_view_but_exact_source_remains_promotable(self) -> None:
        call = self.service.store.append_event(
            kind="tool_call",
            role="assistant",
            content="pytest -vv",
            payload={"_memory_call_id": "call-prompt"},
        )
        output = self.service.store.append_event(
            kind="tool_output",
            role="tool",
            content=self._verbose_failure_output(),
            payload={
                "_memory_call_id": "call-prompt",
                "tool_input": {"command": "pytest -vv"},
            },
        )
        self.service.reduce_tool_output(output)
        packet = self.service.prompts.assemble(token_budget=900, recent_event_count=2)
        self.assertIn(call.id, packet.included_event_ids)
        self.assertIn(output.id, packet.included_event_ids)
        self.assertIn("representation=derived-compact", packet.text)
        self.assertIn("AssertionError", packet.text)
        self.assertNotIn("test_case_0300 PASSED", packet.text)

    def test_high_risk_source_and_diff_outputs_are_not_lossily_reduced(self) -> None:
        for command in ("Get-Content src/core.py", "git diff -- src/core.py"):
            event = self.service.store.append_event(
                kind="tool_output",
                content="\n".join(f"critical source line {index}" for index in range(1000)),
                payload={"tool_input": {"command": command}},
            )
            bundle, views = self.service.reduce_tool_output(event)
            self.assertEqual(views, ())
            self.assertEqual(bundle.compact_critical_recall, 1.0)

    def test_hook_retries_do_not_double_count_reduction_or_provider_usage(self) -> None:
        value = {
            "hook_event_name": "PostToolUse",
            "session_id": "native-retry",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -vv"},
            "tool_response": self._verbose_failure_output(),
            "usage": {"input_tokens": 1200, "output_tokens": 80, "context_tokens": 1200},
        }
        process_hook(self.service, "claude-code", value)
        process_hook(self.service, "claude-code", value)
        report = self.service.usage.report()
        self.assertEqual(report["by_operation"]["tool_output_reduce"]["samples"], 1)
        self.assertEqual(report["by_operation"]["model_usage"]["samples"], 1)
        self.assertGreater(report["totals"]["tool_output_token_savings"], 0)

    def test_governor_uses_versioned_policy_and_applies_audited_actions(self) -> None:
        policy = self.service.store.set_budget_policy(
            context_window_tokens=4000,
            snapshot_ratio=0.20,
            compact_ratio=0.35,
            handoff_ratio=0.60,
            hard_limit_ratio=0.80,
            min_action_interval_events=50,
        )
        event = self.service.store.append_event(
            kind="message", role="assistant", content="context " * 1600
        )
        decision = self.service.governor.evaluate_and_apply(source_event=event)
        self.assertEqual(decision.policy_id, policy.id)
        self.assertIn("snapshot", decision.actions)
        self.assertIn("compact", decision.actions)
        self.assertIn("handoff", decision.actions)
        self.assertIn("handoff_required", decision.actions)
        self.assertIsNotNone(self.service.store.latest_snapshot())
        followup = self.service.store.append_event(kind="message", content="small followup")
        rate_limited = self.service.governor.evaluate_and_apply(source_event=followup)
        self.assertEqual(rate_limited.actions, ())

    def test_global_installers_resolve_user_paths_and_runtime_project(self) -> None:
        parsed = build_parser().parse_args(["install-hooks", "codex", "--global"])
        self.assertTrue(parsed.global_scope)
        root = RUNTIME_ROOT / f"global-install-{uuid.uuid4().hex}"
        repository = root / "workspace"
        nested = repository / "src" / "package"
        (repository / ".git").mkdir(parents=True)
        nested.mkdir(parents=True)
        legacy_database = repository / ".llm-memory" / "memory.db"
        legacy_database.parent.mkdir()
        legacy_database.touch()
        self.assertEqual(resolve_project_database(repository), legacy_database)
        current_database = repository / ".joiny-mnemonic" / "memory.db"
        current_database.parent.mkdir()
        current_database.touch()
        self.assertEqual(resolve_project_database(repository), current_database)
        env = {
            "CLAUDE_CONFIG_DIR": str(root / "claude-home"),
            "CODEX_HOME": str(root / "codex-home"),
            "OPENCODE_CONFIG_DIR": str(root / "opencode-home"),
        }
        results = (
            install_hooks("claude-code", global_scope=True, environ=env, home=root),
            install_hooks("codex", global_scope=True, environ=env, home=root),
            install_hooks("opencode", global_scope=True, environ=env, home=root),
        )
        self.assertEqual(
            Path(results[0].files[0]), root / "claude-home" / "settings.json"
        )
        self.assertEqual(
            Path(results[1].files[0]), root / "codex-home" / "hooks.json"
        )
        self.assertEqual(
            Path(results[2].files[0]),
            root / "opencode-home" / "plugins" / "joiny-mnemonic.js",
        )
        for result in results:
            self.assertEqual(result.scope, "global")
            self.assertIn("--global", result.command)
            self.assertNotIn(".joiny-mnemonic", result.command)
        self.assertEqual(resolve_hook_project({"cwd": str(nested)}), repository)
        self.assertEqual(
            resolve_global_install_path("codex", environ=env, home=root),
            root / "codex-home" / "hooks.json",
        )
        with self.assertRaisesRegex(ValueError, "does not load user-global hooks"):
            install_hooks("openhands", global_scope=True, environ=env, home=root)
    def test_raw_hook_counter_warns_before_native_compaction_and_is_idempotent(self) -> None:
        policy = self.service.store.set_budget_policy(
            context_window_tokens=4000,
            snapshot_ratio=0.20,
            compact_ratio=0.55,
            handoff_ratio=0.75,
            hard_limit_ratio=0.90,
            min_action_interval_events=10,
        )
        first = process_hook(
            self.service,
            "claude-code",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "counter-session",
                "prompt": "inspect the test output",
            },
            token_budget=700,
        )
        self.assertNotIn("CONTEXT CHECKPOINT", first["hookSpecificOutput"]["additionalContext"])
        verbose = "\n".join(
            f"tests/test_bulk.py::test_case_{index:04d} PASSED" for index in range(500)
        )
        delivery = {
            "hook_event_name": "PostToolUse",
            "session_id": "counter-session",
            "tool_use_id": "bulk-tests",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -vv"},
            "tool_response": verbose,
        }
        warned = process_hook(
            self.service, "claude-code", delivery, token_budget=700
        )
        warning = warned["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CONTEXT CHECKPOINT", warning)
        self.assertIn("before native compaction", warning)
        session_id = self.service.store.hook_session(
            "claude-code", "counter-session", branch_id="main"
        )
        cumulative = self.service.store.hook_context_total(
            branch_id="main", session_id=session_id
        )
        self.assertGreaterEqual(
            cumulative,
            int(policy.context_window_tokens * policy.snapshot_ratio),
        )
        compact = next(
            event for event in self.service.store.query_events()
            if event.kind == "tool_output"
        )
        view = self.service.store.get_tool_output_view(compact.id)
        self.assertIsNotNone(view)
        self.assertGreater(cumulative, view.view_tokens)
        retry = process_hook(
            self.service, "claude-code", delivery, token_budget=700
        )
        self.assertIn(
            "CONTEXT CHECKPOINT", retry["hookSpecificOutput"]["additionalContext"]
        )
        self.assertEqual(
            self.service.store.hook_context_total(
                branch_id="main", session_id=session_id
            ),
            cumulative,
        )
        decision = self.service.governor.decide(
            branch_id="main", session_id=session_id
        )
        self.assertEqual(decision.source, "hook-cumulative-raw-estimate")
        self.assertIsNotNone(self.service.store.latest_snapshot())

    def test_task_boundary_has_branch_snapshot_lineage_and_small_resume_packet(self) -> None:
        parent = self.service.store.append_event(kind="message", content="parent project context")
        task = self.service.tasks.start("ISSUE-417", "Repair invoice accounting")
        self.assertTrue(task.branch_id.startswith("task/issue-417-"))
        self.assertIsNotNone(task.snapshot_id)
        visible = self.service.store.query_events(branch_id=task.branch_id)
        self.assertIn(parent.id, {event.id for event in visible})
        self.service.store.append_event(
            branch_id=task.branch_id,
            kind="message",
            role="assistant",
            content="Root cause is decimal rounding in invoice.py:417",
        )
        packet = self.service.tasks.resume("ISSUE-417", token_budget=700)
        self.assertLessEqual(packet.estimated_tokens, 700)
        self.assertIn("Repair invoice accounting", packet.text)
        completed = self.service.tasks.complete("ISSUE-417", note="Regression test added")
        self.assertEqual(completed.version, 2)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(self.service.tasks.list(status="completed"), (completed,))

    def test_native_task_id_binds_followup_hooks_to_task_branch(self) -> None:
        started = {
            "hook_event_name": "SessionStart",
            "session_id": "native-task",
            "task_id": "TASK-9",
            "task_title": "Implement retention benchmark",
        }
        process_hook(self.service, "claude-code", started, token_budget=700)
        task = self.service.store.get_task("TASK-9")
        process_hook(
            self.service,
            "claude-code",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "native-task",
                "prompt": "continue benchmark implementation",
            },
            token_budget=700,
        )
        child_events = self.service.store.query_events(branch_id=task.branch_id)
        self.assertIn("continue benchmark implementation", {event.content for event in child_events})
        main_direct = {
            event.id for event in self.service.store.query_events(branch_id="main")
        }
        prompt_event = next(
            event for event in child_events if event.content == "continue benchmark implementation"
        )
        self.assertNotIn(prompt_event.id, main_direct)


if __name__ == "__main__":
    unittest.main()
