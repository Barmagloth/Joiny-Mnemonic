from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic.prompt import BudgetExceededError
from joiny_mnemonic.retrieval import RetrievalContext
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_ROOT
        self.service = MemoryService(":memory:", project_root=self.root)

    def tearDown(self) -> None:
        self.service.close()

    def test_canonical_events_are_immutable_and_hash_chained(self) -> None:
        first = self.service.store.append_event(kind="message", role="user", content="first")
        second = self.service.store.append_event(kind="tool_output", content="second")
        self.assertEqual(second.previous_hash, first.chain_hash)
        self.assertEqual(self.service.store.verify_chain(), (True, None))
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.store._conn.execute("UPDATE events SET content='changed' WHERE id=?", (first.id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.store._conn.execute("DELETE FROM events WHERE id=?", (first.id,))

    def test_secret_filter_runs_before_durable_write(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        event = self.service.store.append_event(
            kind="message",
            content=f"credential {secret}",
            payload={"api_key": "super-secret-value"},
        )
        self.assertNotIn(secret, event.content)
        self.assertEqual(event.payload["api_key"], "[REDACTED]")
        durable_sql = "\n".join(self.service.store._conn.iterdump())
        self.assertNotIn(secret, durable_sql)
        self.assertNotIn("super-secret-value", durable_sql)

    def test_artifact_is_atomic_immutable_and_secret_filtered(self) -> None:
        artifact = self.service.store.append_artifact(
            name="tool-output.txt",
            data="token=super-secret-value\nresult=ok",
            mime_type="text/plain",
        )
        self.assertNotIn(b"super-secret-value", artifact.data)
        self.assertEqual(self.service.store.get_artifact(artifact.id).data, artifact.data)
        self.assertEqual(self.service.store.verify_chain(), (True, None))
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.store._conn.execute(
                "UPDATE artifacts SET name='changed' WHERE id=?", (artifact.id,)
            )
        with self.assertRaises(ValueError):
            self.service.store.append_artifact(
                name="binary.bin",
                data=b"\x00sk-abcdefghijklmnopqrstuvwxyz123456\x00",
                mime_type="application/octet-stream",
            )

    def test_typed_memory_requires_and_returns_exact_provenance(self) -> None:
        source = self.service.store.append_event(
            kind="message", role="user", content="Use PostgreSQL for durable metadata", files=["design.md"]
        )
        record = self.service.derive_memory(
            memory_type="decision",
            content="The project uses PostgreSQL for durable metadata.",
            summary="Use PostgreSQL",
            source_event_ids=[source.id],
            files=["design.md"],
            risk=0.7,
        )
        self.assertEqual(self.service.exact_source(record.id), [source])
        hits = self.service.retrieval.search(
            RetrievalContext(query="PostgreSQL metadata", file="design.md", limit=5)
        )
        memory_hit = next(hit for hit in hits if hit.id == record.id)
        self.assertEqual(memory_hit.source_event_ids, (source.id,))
        self.assertEqual(self.service.retrieval.promote_to_source(memory_hit)[0].content, source.content)
        with self.assertRaises(ValueError):
            self.service.derive_memory(
                memory_type="fact", content="invented", source_event_ids=["evt_missing"]
            )

    def test_provenance_must_be_visible_in_target_branch(self) -> None:
        initial = self.service.store.append_event(kind="message", content="before fork")
        self.service.store.create_branch("child")
        after_fork = self.service.store.append_event(kind="message", content="parent later")
        self.service.derive_memory(
            memory_type="fact",
            content="visible inherited fact",
            source_event_ids=[initial.id],
            branch_id="child",
        )
        with self.assertRaisesRegex(ValueError, "outside the target branch lineage"):
            self.service.derive_memory(
                memory_type="fact",
                content="must be rejected",
                source_event_ids=[after_fork.id],
                branch_id="child",
            )

    def test_recent_transcript_keeps_tool_call_and_output_atomic(self) -> None:
        call = self.service.store.append_event(
            kind="tool_call", content="read file", payload={"_memory_call_id": "call-1"}
        )
        output = self.service.store.append_event(
            kind="tool_output", content="file contents", payload={"_memory_call_id": "call-1"}
        )
        packet = self.service.prompts.assemble(token_budget=350, recent_event_count=2)
        self.assertIn(call.id, packet.included_event_ids)
        self.assertIn(output.id, packet.included_event_ids)

        service = MemoryService(":memory:", project_root=self.root)
        try:
            large_call = service.store.append_event(
                kind="tool_call", content="x" * 900, payload={"_memory_call_id": "call-2"}
            )
            small_output = service.store.append_event(
                kind="tool_output", content="short", payload={"_memory_call_id": "call-2"}
            )
            orphan = service.store.append_event(
                kind="tool_output", content="orphan", payload={"_memory_call_id": "missing"}
            )
            constrained = service.prompts.assemble(token_budget=180, recent_event_count=3)
            self.assertNotIn(large_call.id, constrained.included_event_ids)
            self.assertNotIn(small_output.id, constrained.included_event_ids)
            self.assertNotIn(orphan.id, constrained.included_event_ids)
        finally:
            service.close()

    def test_fts_retrieval_avoids_full_python_history_scan(self) -> None:
        source = self.service.store.append_event(
            kind="message", content="Nebula migration uses Tantivy", files=["search.md"]
        )
        record = self.service.derive_memory(
            memory_type="decision",
            content="Nebula migration uses Tantivy.",
            summary="Tantivy for Nebula",
            source_event_ids=[source.id],
            files=["search.md"],
        )
        self.assertTrue(self.service.store.fts_enabled)
        with (
            patch.object(self.service.store, "list_memories", side_effect=AssertionError("scan")),
            patch.object(self.service.store, "query_events", side_effect=AssertionError("scan")),
        ):
            hits = self.service.search(
                query="Nebula Tantivy",
                memory_types=("decision",),
                file="search.md",
                include_events=True,
                semantic=False,
            )
        hit = next(item for item in hits if item.id == record.id)
        self.assertEqual(hit.metadata["retrieval_backend"], "fts5-bm25")

    def test_fts_retrieval_respects_branch_fork_visibility(self) -> None:
        visible_source = self.service.store.append_event(
            kind="message", content="Orchid is visible before fork"
        )
        visible = self.service.derive_memory(
            memory_type="fact", content="Orchid visible", source_event_ids=[visible_source.id]
        )
        self.service.store.create_branch("child")
        hidden_source = self.service.store.append_event(
            kind="message", content="Obsidian exists only on parent after fork"
        )
        hidden = self.service.derive_memory(
            memory_type="fact", content="Obsidian hidden", source_event_ids=[hidden_source.id]
        )
        child_visible = self.service.search(query="Orchid", branch_id="child")
        child_hidden = self.service.search(query="Obsidian", branch_id="child")
        self.assertIn(visible.id, {item.id for item in child_visible})
        self.assertNotIn(hidden.id, {item.id for item in child_hidden})

    def test_active_blocks_are_versioned_and_never_compacted(self) -> None:
        first = self.service.store.set_active_block("instructions", "Always run integrity tests.")
        second = self.service.store.set_active_block("instructions", "Always run all integrity tests.")
        self.assertEqual(second.version, 2)
        self.assertEqual(second.supersedes_id, first.id)
        packet = self.service.prompts.assemble(token_budget=180, recent_event_count=0)
        self.assertIn("Always run all integrity tests.", packet.text)
        self.assertLessEqual(packet.estimated_tokens, 180)
        with self.assertRaises(BudgetExceededError):
            self.service.prompts.assemble(token_budget=3)
        with self.assertRaises(ValueError):
            self.service.store.set_active_block("goal", "x" * 4000)
        self.assertNotIn("goal", self.service.store.get_active_blocks())

    def test_branch_lineage_hides_parent_updates_after_fork(self) -> None:
        old = self.service.store.set_active_block("goal", "old goal")
        fork_seq = self.service.store.query_events()[-1].seq
        self.service.store.create_branch("experiment", fork_event_seq=fork_seq)
        self.service.store.set_active_block("goal", "new main goal")
        child = self.service.store.get_active_blocks(branch_id="experiment")["goal"]
        self.assertEqual(child.id, old.id)
        child_event = self.service.store.append_event(
            branch_id="experiment", kind="message", content="child-only"
        )
        child_events = self.service.store.query_events(branch_id="experiment")
        self.assertIn(child_event.id, {event.id for event in child_events})
        self.assertNotIn("new main goal", {event.content for event in child_events})


class CrashDurabilityTest(unittest.TestCase):
    def test_committed_event_survives_abrupt_process_exit(self) -> None:
        database = RUNTIME_ROOT / "crash.db"
        try:
            script = (
                "from joiny_mnemonic.storage import MemoryStore; import os,sys; "
                "s=MemoryStore(sys.argv[1]); "
                "s.append_event(kind='message', content='committed-before-crash'); "
                "os._exit(19)"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run([sys.executable, "-c", script, str(database)], env=env)
            self.assertEqual(result.returncode, 19)
            from joiny_mnemonic.storage import MemoryStore

            store = MemoryStore(database)
            try:
                events = store.query_events()
                self.assertEqual(events[-1].content, "committed-before-crash")
                self.assertEqual(store.verify_chain(), (True, None))
            finally:
                store.close()
        finally:
            pass


if __name__ == "__main__":
    unittest.main()
