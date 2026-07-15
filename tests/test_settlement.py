"""task6.md 6B: general candidate settlement.

The extraction-candidate ledger generalizes into the system's settlement
journal: every autonomous state change is a candidate with kind, evidence
strength, consume-once transitions and a lossless undo. Cheap undo is what
licenses automation-first defaults.
"""
from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from joiny_mnemonic import storage as storage_module
from joiny_mnemonic.hooks import process_hook
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class SettlementLedgerCase(unittest.TestCase):
    """Storage primitives: consume-once, idempotence, fail-closed."""

    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.store = self.service.store
        self.anchor = self.store.append_event(
            kind="message", role="user", content="anchor"
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_schema_v9_settlement_scaffolding(self) -> None:
        version = self.store._conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(version, str(storage_module.CURRENT_SCHEMA_VERSION))
        config = self.store._conn.execute(
            "SELECT config_hash FROM extractor_configs WHERE config_hash=?",
            (storage_module.SETTLEMENT_CONFIG_HASH,),
        ).fetchone()
        self.assertIsNotNone(config)
        # Legacy extraction rows keep their kind via the column default.
        columns = {
            row["name"]: row
            for row in self.store._conn.execute(
                "PRAGMA table_info(extraction_candidates)"
            ).fetchall()
        }
        self.assertIn("candidate_kind", columns)
        self.assertEqual(columns["candidate_kind"]["dflt_value"], "'extraction'")

    def test_create_is_idempotent_per_source_event(self) -> None:
        first_id, created, status = self.store.create_settlement_candidate(
            kind="task_closure", content="создать файл x.md",
            source_event_id=self.anchor.id, strength="strong",
        )
        self.assertTrue(created)
        self.assertEqual(status, "pending")
        again_id, created_again, status_again = self.store.create_settlement_candidate(
            kind="task_closure", content="создать файл x.md",
            source_event_id=self.anchor.id, strength="strong",
        )
        self.assertEqual(again_id, first_id)
        self.assertFalse(created_again)
        self.assertEqual(status_again, "pending")

    def test_extraction_kind_is_rejected_on_both_sides(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_settlement_candidate(
                kind="extraction", content="x", source_event_id=self.anchor.id
            )

    def test_consume_once_flow(self) -> None:
        candidate_id, _, _ = self.store.create_settlement_candidate(
            kind="task_closure", content="entry",
            source_event_id=self.anchor.id,
        )
        # pending -> reverted is not a legal edge: fail closed.
        with self.assertRaises(ValueError):
            self.store.settle_candidate(
                candidate_id, "reverted",
                source_event_id=self.anchor.id, actor="system", rule_id="t",
            )
        transition = self.store.settle_candidate(
            candidate_id, "applied",
            source_event_id=self.anchor.id, actor="system", rule_id="t",
        )
        self.assertIsNotNone(transition)
        # Idempotent repeat: consumed, returns None, adds no transition.
        self.assertIsNone(
            self.store.settle_candidate(
                candidate_id, "applied",
                source_event_id=self.anchor.id, actor="system", rule_id="t",
            )
        )
        self.store.settle_candidate(
            candidate_id, "reverted",
            source_event_id=self.anchor.id, actor="system", rule_id="t",
        )
        # Terminal: reverted accepts nothing further.
        with self.assertRaises(ValueError):
            self.store.settle_candidate(
                candidate_id, "applied",
                source_event_id=self.anchor.id, actor="system", rule_id="t",
            )
        with self.assertRaises(ValueError):
            self.store.settle_candidate(
                candidate_id, "nonsense",
                source_event_id=self.anchor.id, actor="system", rule_id="t",
            )
        listed = self.store.list_settlement_candidates(kind="task_closure")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "reverted")
        self.assertEqual(listed[0]["source_event_id"], self.anchor.id)


class SettlementFlowCase(unittest.TestCase):
    """End-to-end reconciler flows over the trusted host channel."""

    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.store = self.service.store
        self._receipt = 0

    def tearDown(self) -> None:
        self.service.close()

    def _open_task(self, text: str):
        self._receipt += 1
        events, _ = self.store.append_host_events_once(
            f"receipt:{text}:{self._receipt}",
            [{"kind": "message", "role": "user", "content": f"TODO: {text}", "payload": {}}],
            adapter="claude-code",
        )
        self.service.consolidator.consolidate_event(self.service, events[0])
        return events[0]

    def _write_evidence(self, path: str):
        self._receipt += 1
        events, _ = self.store.append_host_events_once(
            f"evidence:{path}:{self._receipt}",
            [
                {
                    "kind": "tool_output", "role": "tool",
                    "content": f'{{"type": "create", "filePath": "{path}"}}',
                    "payload": {
                        "tool_name": "Write",
                        "hook_event_name": "PostToolUse",
                        "tool_response": {"type": "create"},
                    },
                    "files": [path],
                }
            ],
            adapter="claude-code",
        )
        return events[0]

    def test_undo_restores_line_memory_and_ledger(self) -> None:
        self.service.initialize_project()
        self._open_task("создать файл undoable.md")
        self._write_evidence("R:\\Projects\\GPTShared\\undoable.md")
        summary = self.service.reconciler.reconcile()
        candidate_id = summary["auto_closed"][0]["candidate_id"]

        result = self.service.reconciler.undo_closure(candidate_id)
        self.assertFalse(result["already_reverted"])
        self.assertEqual(result["entry"], "создать файл undoable.md")
        block = self.store.get_active_blocks()["open_tasks"]
        self.assertIn("создать файл undoable.md", block.content)
        # The task memory round-trips: completed -> reopened, all versions kept.
        records = self.store.list_memories(memory_types=("task",))
        self.assertEqual(records[0].metadata.get("status"), "reopened")
        history = self.store.list_memories(
            memory_types=("task",), include_superseded=True
        )
        statuses = {r.metadata.get("status") for r in history}
        self.assertIn("completed", statuses)
        # Applied/reverted receipts record audit level, never OS authority.
        applied = self.store.events_by_operation("task_closure_applied")
        reverted = self.store.events_by_operation("task_closure_reverted")
        self.assertEqual(applied[0].payload["enforcement_level"], "recorded_only")
        self.assertEqual(reverted[0].payload["enforcement_level"], "recorded_only")
        # Second undo is a no-op receipt, not an error.
        again = self.service.reconciler.undo_closure(candidate_id)
        self.assertTrue(again["already_reverted"])
        # Reverted is terminal: the same evidence never re-applies.
        after = self.service.reconciler.reconcile()
        self.assertEqual(after["closed"], 0)
        self.assertIn(
            "создать файл undoable.md",
            self.store.get_active_blocks()["open_tasks"].content,
        )
        # A reverted closure must not advertise a stale undo in the digest.
        packet = self.service.resume(token_budget=1500)
        self.assertNotIn("AUTO-CLOSED RECENTLY", packet.text)

    def test_marker_reassertion_contests_the_closure(self) -> None:
        self.service.initialize_project()
        self._open_task("создать файл contested.md")
        self._write_evidence("R:\\Projects\\GPTShared\\contested.md")
        summary = self.service.reconciler.reconcile()
        candidate_id = summary["auto_closed"][0]["candidate_id"]

        # The user re-adds the entry: that marker IS the correction signal.
        self._open_task("создать файл contested.md")
        candidates = {
            item["id"]: item
            for item in self.store.list_settlement_candidates(kind="task_closure")
        }
        self.assertEqual(candidates[candidate_id]["status"], "contested")
        after = self.service.reconciler.reconcile()
        self.assertEqual(after["closed"], 0)
        self.assertIn(
            "создать файл contested.md",
            self.store.get_active_blocks()["open_tasks"].content,
        )

    def test_missing_evidence_file_auto_reverts_inside_project_root(self) -> None:
        root = RUNTIME_ROOT / f"settle-{uuid.uuid4().hex}"
        root.mkdir()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        service = MemoryService(":memory:", project_root=root)
        self.addCleanup(service.close)
        service.initialize_project()
        target = root / "инвойс.md"
        target.write_text("real", encoding="utf-8")

        events, _ = service.store.append_host_events_once(
            "receipt:inv",
            [{"kind": "message", "role": "user", "content": "TODO: создать файл инвойс.md", "payload": {}}],
            adapter="claude-code",
        )
        service.consolidator.consolidate_event(service, events[0])
        service.store.append_host_events_once(
            "evidence:inv",
            [
                {
                    "kind": "tool_output", "role": "tool",
                    "content": '{"type": "create"}',
                    "payload": {
                        "tool_name": "Write",
                        "hook_event_name": "PostToolUse",
                        "tool_response": {"type": "create"},
                    },
                    "files": [str(target)],
                }
            ],
            adapter="claude-code",
        )
        summary = service.reconciler.reconcile()
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(summary["invalidated"], [])
        candidate_id = summary["auto_closed"][0]["candidate_id"]

        # Evidence vanishes -> the write path catches the system's own mistake.
        target.unlink()
        second = service.reconciler.reconcile()
        self.assertEqual(len(second["invalidated"]), 1)
        self.assertEqual(second["invalidated"][0]["candidate_id"], candidate_id)
        self.assertEqual(second["closed"], 0)
        self.assertIn(
            "создать файл инвойс.md",
            service.store.get_active_blocks()["open_tasks"].content,
        )
        candidates = {
            item["id"]: item
            for item in service.store.list_settlement_candidates(kind="task_closure")
        }
        self.assertEqual(candidates[candidate_id]["status"], "reverted")
        # hygiene_findings only reads the revert event — twice is identical.
        first_read = service.reconciler.hygiene_findings()
        second_read = service.reconciler.hygiene_findings()
        kinds = {item["finding"] for item in first_read}
        self.assertIn("closure_evidence_invalidated", kinds)
        self.assertEqual(first_read, second_read)

    def test_question_marker_creates_block_change_candidate(self) -> None:
        self.service.initialize_project()
        self._receipt += 1
        events, _ = self.store.append_host_events_once(
            f"receipt:question:{self._receipt}",
            [
                {
                    "kind": "message", "role": "user",
                    "content": "DECISION: что сделать дальше?", "payload": {},
                }
            ],
            adapter="claude-code",
        )
        self.service.consolidator.consolidate_event(self.service, events[0])
        candidates = self.store.list_settlement_candidates(kind="block_change")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["status"], "pending")
        self.assertEqual(candidates[0]["normalized_content"], "что сделать дальше?")
        self.assertEqual(candidates[0]["memory_type"], "decisions")
        self.assertEqual(candidates[0]["evidence_zone"], "block_change:weak")

    def test_resume_packet_reports_recent_auto_closures_with_undo(self) -> None:
        self.service.initialize_project()
        self._open_task("создать файл digest.md")
        self._write_evidence("R:\\Projects\\GPTShared\\digest.md")
        summary = self.service.reconciler.reconcile()
        candidate_id = summary["auto_closed"][0]["candidate_id"]
        packet = self.service.resume(token_budget=1500)
        self.assertIn("STATE MAINTENANCE - AUTO-CLOSED RECENTLY", packet.text)
        self.assertIn("создать файл digest.md", packet.text)
        self.assertIn(f"joiny-mnemonic candidates undo {candidate_id}", packet.text)

    def test_hook_delivery_surfaces_auto_closure_as_system_message(self) -> None:
        self.service.initialize_project()
        self._open_task("создать файл notice.md")
        output = process_hook(
            self.service,
            "claude-code",
            {
                "hook_event_name": "PostToolUse",
                "session_id": "settle-1",
                "tool_use_id": "call-settle-1",
                "tool_name": "Write",
                "tool_input": {"file_path": "R:\\Projects\\GPTShared\\notice.md"},
                "tool_response": {"type": "create"},
            },
        )
        message = output.get("systemMessage", "")
        self.assertIn("auto-closed", message)
        self.assertIn("создать файл notice.md", message)
        self.assertIn("candidates undo", message)


if __name__ == "__main__":
    unittest.main()
