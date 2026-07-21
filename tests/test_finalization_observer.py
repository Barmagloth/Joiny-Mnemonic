from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.finalization_observer import (
    classify_finalization_text,
    observe_finalizations,
)
from joiny_mnemonic.service import MemoryService


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class FinalizationGrammarTest(unittest.TestCase):
    def test_exact_grammar_and_hostile_markdown_lookalikes(self) -> None:
        fence = chr(96) * 3
        content = "\n".join([
            "[FACT] CONFIRMED: Решение принято.",
            " [FACT] CONFIRMED: indented",
            "[FACT] CONFIRMED:no separator",
            "> [FACT] CONFIRMED: quoted",
            fence + "text",
            "[FACT] CONFIRMED: fenced",
            fence,
            "[TODO] DEFERRED: Вернуться после dogfood.",
        ])
        result = classify_finalization_text(content)
        self.assertEqual(
            [(item["type"], item["status"]) for item in result["valid"]],
            [("FACT", "CONFIRMED"), ("TODO", "DEFERRED")],
        )
        self.assertEqual(len(result["malformed"]), 2)
        self.assertEqual(len(result["excluded"]), 2)

    def test_empty_trailing_and_oversized_text_fail_closed(self) -> None:
        result = classify_finalization_text("\n".join([
            "[FACT] CONFIRMED: ",
            "[FACT] CONFIRMED: trailing ",
            "[FACT] CONFIRMED: " + "x" * 2001,
        ]))
        self.assertEqual(result["valid"], [])
        self.assertEqual(len(result["malformed"]), 3)


class FinalizationObserverTest(unittest.TestCase):
    def test_observer_is_read_only_and_reports_real_stop_events(self) -> None:
        database = RUNTIME_ROOT / f"finalization-observer-{uuid.uuid4().hex}.db"
        self.addCleanup(lambda: database.unlink(missing_ok=True))
        with MemoryService(database, project_root=RUNTIME_ROOT) as service:
            valid = service.store.append_host_event(
                adapter="codex",
                kind="message",
                role="assistant",
                content="[FACT] CONFIRMED: Observer remains read-only.",
                payload={"hook_event_name": "Stop"},
            )
            untagged = service.store.append_host_event(
                adapter="claude-code",
                kind="message",
                role="assistant",
                content="Should we use a different extractor?",
                payload={"hook_event_name": "Stop"},
            )
            malformed = service.store.append_host_event(
                adapter="codex",
                kind="message",
                role="assistant",
                content="[FACT] CONFIRMED:no separator",
                payload={"hook_event_name": "Stop"},
            )
            excluded = service.store.append_host_event(
                adapter="claude-code",
                kind="message",
                role="assistant",
                content="> [FACT] CONFIRMED: quoted lookalike",
                payload={"hook_event_name": "Stop"},
            )
            service.store.append_event(
                kind="message",
                role="assistant",
                content="[FACT] CONFIRMED: public API forgery",
                payload={"hook_event_name": "Stop"},
            )

            before = {
                "chain": service.store.chain_checkpoint(),
                "memories": tuple(service.store.list_memories()),
                "blocks": service.store.get_active_blocks(),
                "tasks": service.store.list_tasks(),
            }
            report = observe_finalizations(database)
            after = {
                "chain": service.store.chain_checkpoint(),
                "memories": tuple(service.store.list_memories()),
                "blocks": service.store.get_active_blocks(),
                "tasks": service.store.list_tasks(),
            }

        self.assertEqual(before, after)
        self.assertEqual(report["host_assistant_stop_events"], 4)
        self.assertEqual(report["events_with_valid_tags"], 1)
        self.assertEqual(report["events_without_valid_tags"], 3)
        self.assertEqual(report["events_with_malformed_lookalikes"], 1)
        self.assertEqual(report["events_with_excluded_lookalikes"], 1)
        self.assertEqual(report["valid_tag_count"], 1)
        self.assertEqual(report["by_type"], {"FACT": 1})
        self.assertEqual(report["by_status"], {"CONFIRMED": 1})
        self.assertEqual(
            report["by_adapter"],
            {
                "claude-code": {
                    "events_with_excluded_lookalikes": 1,
                    "events_without_valid_tags": 2,
                    "stop_events": 2,
                    "valid_tags": 0,
                },
                "codex": {
                    "events_with_malformed_lookalikes": 1,
                    "events_with_valid_tags": 1,
                    "events_without_valid_tags": 1,
                    "stop_events": 2,
                    "valid_tags": 1,
                },
            },
        )
        self.assertEqual(report["event_ids"]["valid"], [valid.id])
        self.assertIn(untagged.id, report["event_ids"]["untagged"])
        self.assertEqual(report["event_ids"]["malformed"], [malformed.id])
        self.assertEqual(report["event_ids"]["excluded"], [excluded.id])
        self.assertTrue(report["observation_only"])
        self.assertFalse(report["materialized"])

    def test_missing_database_and_negative_sample_limit_fail(self) -> None:
        with self.assertRaises(FileNotFoundError):
            observe_finalizations(RUNTIME_ROOT / "missing-observer.db")
        database = RUNTIME_ROOT / f"observer-limit-{uuid.uuid4().hex}.db"
        self.addCleanup(lambda: database.unlink(missing_ok=True))
        with MemoryService(database, project_root=RUNTIME_ROOT):
            with self.assertRaises(ValueError):
                observe_finalizations(database, sample_limit=-1)

    def test_host_instruction_files_share_observation_contract(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(agents, claude)
        self.assertIn("[TYPE] STATUS: self-contained outcome", agents)
        self.assertIn("unanswered question", agents)
        self.assertIn("observation-only", agents)


if __name__ == "__main__":
    unittest.main()
