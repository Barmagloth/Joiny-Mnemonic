from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.context_limits import load_builtin_profiles
from joiny_mnemonic.governor import BudgetGovernor
from joiny_mnemonic.hooks import install_hooks, process_hook
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class ContextLimitsTest(unittest.TestCase):
    def test_seven_bundled_profiles_and_agent_specific_install_config(self) -> None:
        bundled = load_builtin_profiles()
        self.assertEqual(len(bundled["profiles"]), 7)
        root = RUNTIME_ROOT / f"context-profiles-{uuid.uuid4().hex}"
        root.mkdir(parents=True)

        claude = install_hooks(
            "claude-code",
            root,
            profile="claude-sonnet-4.6",
            recommended_handoff_tokens=180_000,
        )
        codex = install_hooks(
            "codex",
            root,
            profile="gpt-5.2-codex",
            recommended_handoff_tokens=120_000,
        )
        self.assertEqual(claude.limits_file, codex.limits_file)
        document = json.loads(Path(claude.limits_file).read_text(encoding="utf-8"))
        self.assertEqual(document["agents"]["claude-code"]["profile"], "claude-sonnet-4.6")
        self.assertEqual(document["agents"]["codex"]["profile"], "gpt-5.2-codex")

        with MemoryService(":memory:", project_root=root) as service:
            claude_policy = service.budget_policy(agent="claude-code")
            codex_policy = service.budget_policy(agent="codex")
            self.assertEqual(claude_policy.context_window_tokens, 1_000_000)
            self.assertEqual(codex_policy.context_window_tokens, 400_000)
            self.assertEqual(BudgetGovernor.thresholds(claude_policy)["handoff"], 180_000)
            self.assertEqual(BudgetGovernor.thresholds(codex_policy)["handoff"], 120_000)
            self.assertNotEqual(claude_policy.id, codex_policy.id)

    def test_snapshot_checkpoint_does_not_recommend_handoff_early(self) -> None:
        root = RUNTIME_ROOT / f"context-thresholds-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        with MemoryService(":memory:", project_root=root) as service:
            service.context_limits.configure_agent(
                "claude-code",
                profile="custom",
                overrides={
                    "context_window_tokens": 4_000,
                    "snapshot_ratio": 0.20,
                    "compact_ratio": 0.40,
                    "handoff_ratio": 0.60,
                    "hard_limit_ratio": 0.90,
                    "recommended_handoff_tokens": 2_400,
                    "reserve_tokens": 200,
                    "min_action_interval_events": 0,
                },
            )
            checkpoint = process_hook(
                service,
                "claude-code",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "threshold-session",
                    "prompt": "word " * 700,
                },
                token_budget=700,
            )["hookSpecificOutput"]["additionalContext"]
            self.assertIn("[CONTEXT CHECKPOINT]", checkpoint)
            self.assertIn("Handoff is not recommended until", checkpoint)
            self.assertNotIn("[CONTEXT HANDOFF RECOMMENDED]", checkpoint)
            self.assertNotIn("Start a new session", checkpoint)

            handoff = process_hook(
                service,
                "claude-code",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "threshold-session",
                    "message_id": "second",
                    "prompt": "word " * 1_200,
                },
                token_budget=700,
            )["hookSpecificOutput"]["additionalContext"]
            self.assertIn("[CONTEXT HANDOFF RECOMMENDED]", handoff)
            self.assertNotIn("Joiny-Mnemonic will preserve", handoff)

    def test_reinstall_without_limit_arguments_preserves_manual_values(self) -> None:
        root = RUNTIME_ROOT / f"context-reinstall-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        first = install_hooks(
            "codex",
            root,
            profile="custom",
            context_window_tokens=90_000,
            recommended_handoff_tokens=40_000,
            reserve_tokens=10_000,
        )
        second = install_hooks("codex", root)
        self.assertEqual(first.profile, "custom")
        self.assertEqual(second.profile, "custom")
        document = json.loads(Path(second.limits_file).read_text(encoding="utf-8"))
        limits = document["agents"]["codex"]["limits"]
        self.assertEqual(limits["context_window_tokens"], 90_000)
        self.assertEqual(limits["recommended_handoff_tokens"], 40_000)


if __name__ == "__main__":
    unittest.main()
