from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic import storage as storage_module
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.temporal import (
    TEMPORAL_PROJECTION_CODE_VERSION,
    TemporalValidationError,
)


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class BitemporalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.store = self.service.store

    def tearDown(self) -> None:
        self.service.close()

    def _event(self, content: str, **values):
        return self.store.append_event(kind="message", role="user", content=content, **values)

    def _derive(self, content: str, source, **values):
        return self.service.derive_memory(
            memory_type="fact",
            content=content,
            source_event_ids=(source.id,),
            **values,
        )

    def test_temporal_fields_stored_normalized_and_replayed(self) -> None:
        source = self._event("We adopted SQLite in March 2026.")
        record = self._derive(
            "storage engine is SQLite",
            source,
            valid_from="2026-03",
            temporal_expression="in March 2026",
        )
        self.assertEqual(record.valid_from_precision, "month")
        self.assertIsNone(record.valid_to)
        stored = self.store.get_memory(record.id)
        self.assertEqual(stored.valid_from, record.valid_from)
        self.assertEqual(stored.temporal_expression, "in March 2026")
        # Canonical derive event carries the normalized fields for replay.
        provenance_events = self.store.query_events(kinds=("state",))
        payloads = [
            event.payload for event in provenance_events
            if event.payload.get("memory_id") == record.id
        ]
        self.assertEqual(payloads[0]["valid_from"], record.valid_from)
        self.assertEqual(payloads[0]["valid_from_precision"], "month")

    def test_invalid_temporal_input_is_rejected(self) -> None:
        source = self._event("bad interval")
        with self.assertRaises(TemporalValidationError):
            self._derive("x", source, valid_from="2026-05", valid_to="2026-03")
        with self.assertRaises(TemporalValidationError):
            self._derive("y", source, valid_from="next sprint")

    def test_relative_date_resolves_against_source_event_time(self) -> None:
        source = self._event("we switched yesterday")
        record = self._derive("switched to WAL", source, valid_from="yesterday")
        anchor = datetime.fromisoformat(source.created_at)
        expected_day = (anchor - timedelta(days=1)).date()
        self.assertEqual(record.valid_from_precision, "day")
        self.assertEqual(datetime.fromisoformat(record.valid_from).date(), expected_day)

    def test_current_filter_partitions_validity(self) -> None:
        source = self._event("validity partitions")
        self._derive("expired fact", source, valid_from="2020", valid_to="2021")
        open_record = self._derive("open fact", source, valid_from="2020")
        unknown_record = self._derive("plain fact", source)
        future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        current_record = self._derive(
            "proven fact", source, valid_from="2020", valid_to=future
        )

        hits = self.service.search(current=True, include_events=False, limit=20)
        by_id = {hit.id: hit for hit in hits}
        self.assertIn(current_record.id, by_id)
        self.assertIn(open_record.id, by_id)
        self.assertNotIn(unknown_record.id, by_id)
        self.assertEqual(by_id[current_record.id].metadata["validity_status"], "current")
        self.assertEqual(by_id[open_record.id].metadata["validity_status"], "current_open")
        self.assertEqual(
            by_id[current_record.id].metadata["temporal_projection_code_version"],
            TEMPORAL_PROJECTION_CODE_VERSION,
        )

        widened = self.service.search(
            current=True, include_unknown_validity=True, include_events=False, limit=20
        )
        widened_ids = {hit.id for hit in widened}
        self.assertIn(unknown_record.id, widened_ids)

    def test_valid_at_uses_half_open_interval(self) -> None:
        source = self._event("interval boundaries")
        record = self._derive(
            "bounded fact", source,
            valid_from="2026-03-01T00:00:00+00:00",
            valid_to="2026-04-01T00:00:00+00:00",
        )
        inside = self.service.search(
            valid_at="2026-03-15T00:00:00+00:00", include_events=False, limit=10
        )
        self.assertIn(record.id, {hit.id for hit in inside})
        at_end = self.service.search(
            valid_at="2026-04-01T00:00:00+00:00", include_events=False, limit=10
        )
        self.assertNotIn(record.id, {hit.id for hit in at_end})
        at_start = self.service.search(
            valid_at="2026-03-01T00:00:00+00:00", include_events=False, limit=10
        )
        self.assertIn(record.id, {hit.id for hit in at_start})

    def test_combined_bitemporal_query_respects_retroactive_correction(self) -> None:
        """task4.md flagship test: valid_at answers differ across known_at
        cutoffs after a retroactive correction, including the predecessor's
        effective interval appearing open at K1 and closed at K2."""
        source = self._event("policy history")
        original = self._derive(
            "retention policy is 30 days", source, valid_from="2026-01"
        )
        k1 = datetime.now(UTC).isoformat()
        time.sleep(0.01)
        correction_source = self._event("Correction: retention became 90 days in June.")
        corrected = self.service.derive_memory(
            memory_type="fact",
            content="retention policy is 90 days",
            source_event_ids=(correction_source.id,),
            supersedes_id=original.id,
            valid_from="2026-06",
        )
        k2 = datetime.now(UTC).isoformat()

        # As known at K1: only the original exists; its end is open.
        at_k1 = self.service.search(
            valid_at="2026-07-01", known_at=k1, include_events=False,
            include_unknown_validity=True, limit=10,
        )
        k1_ids = {hit.id: hit for hit in at_k1}
        self.assertIn(original.id, k1_ids)
        self.assertNotIn(corrected.id, k1_ids)
        self.assertNotIn("effective_valid_to", k1_ids[original.id].metadata)

        # As known at K2: the successor closes the original's effective
        # interval, so July validity belongs to the corrected version only.
        at_k2 = self.service.search(
            valid_at="2026-07-01", known_at=k2, include_events=False,
            include_unknown_validity=True, limit=10,
        )
        k2_ids = {hit.id for hit in at_k2}
        self.assertIn(corrected.id, k2_ids)
        self.assertNotIn(original.id, k2_ids)

        # History mode at K2 exposes the superseded original with its
        # projection-only effective end; the stored row is untouched.
        history = self.service.search(
            known_at=k2, history=True, include_events=False,
            include_unknown_validity=True, limit=10,
        )
        history_hits = {hit.id: hit for hit in history}
        self.assertIn(original.id, history_hits)
        self.assertEqual(
            history_hits[original.id].metadata["superseded_by"], corrected.id
        )
        self.assertEqual(
            history_hits[original.id].metadata["effective_valid_to"],
            corrected.valid_from,
        )
        self.assertIsNone(self.store.get_memory(original.id).valid_to)

    def test_validity_status_stays_anchored_to_now_under_valid_at(self) -> None:
        source = self._event("status anchoring")
        # Instant bounds: year-precision bounds would make mid-year containment
        # honestly UNKNOWN (the fact may have started any time that year).
        record = self._derive(
            "old policy", source,
            valid_from="2020-01-01T00:00:00+00:00",
            valid_to="2021-01-01T00:00:00+00:00",
        )
        hits = self.service.search(
            valid_at="2020-06-15T00:00:00+00:00", include_events=False, limit=10
        )
        target = next(hit for hit in hits if hit.id == record.id)
        # The fact matched the asked instant, but its trust level for *now*
        # is expired — never "current" (task4.md invariant 4).
        self.assertEqual(target.metadata["temporal_match"], "definite")
        self.assertEqual(target.metadata["validity_status"], "expired")

    def test_known_at_query_finds_k_era_version_with_divergent_text(self) -> None:
        source = self._event("the target moved with renamed text")
        original = self.service.derive_memory(
            memory_type="fact",
            content="deployment target is Vercel",
            source_event_ids=(source.id,),
        )
        k1 = datetime.now(UTC).isoformat()
        time.sleep(0.01)
        successor_source = self._event("hosting migrated")
        self.service.derive_memory(
            memory_type="fact",
            content="hosting now lives on the Fly.io platform",
            source_event_ids=(successor_source.id,),
            supersedes_id=original.id,
        )
        # The successor's text no longer matches the query; the K-era version
        # must still be reachable through the same query (review finding F1).
        hits = self.service.search(
            query="Vercel", known_at=k1,
            include_events=False, include_unknown_validity=True, limit=10,
        )
        self.assertIn(original.id, {hit.id for hit in hits})

    def test_known_at_replays_superseded_version_found_via_fts(self) -> None:
        source = self._event("the deployment target moved")
        original = self.service.derive_memory(
            memory_type="fact",
            content="deployment target is Vercel",
            source_event_ids=(source.id,),
        )
        k1 = datetime.now(UTC).isoformat()
        time.sleep(0.01)
        successor_source = self._event("moved to Fly.io")
        successor = self.service.derive_memory(
            memory_type="fact",
            content="deployment target is Fly.io",
            source_event_ids=(successor_source.id,),
            supersedes_id=original.id,
        )
        hits = self.service.search(
            query="deployment target", known_at=k1,
            include_events=False, include_unknown_validity=True, limit=10,
        )
        ids = {hit.id for hit in hits}
        self.assertIn(original.id, ids, "version live at K must replace its successor")
        self.assertNotIn(successor.id, ids)

    def test_known_at_is_deterministic_when_wall_clock_disagrees_with_seq(self) -> None:
        early = self._event("first admitted")
        base = datetime.fromisoformat(early.created_at)
        skewed = (base - timedelta(seconds=30)).astimezone(UTC).isoformat(
            timespec="microseconds"
        )
        with patch.object(storage_module, "_now", return_value=skewed):
            backwards = self._event("admitted second with earlier clock")
        self.assertGreater(backwards.seq, early.seq)
        cutoff = self.store.known_at_cutoff_seq(early.created_at)
        # Both events have created_at <= K; the canonical order is seq, so the
        # cutoff deterministically includes the later-admitted event.
        self.assertEqual(cutoff, backwards.seq)

    def test_known_at_respects_branch_fork_cutoff(self) -> None:
        before_fork = self._event("shared history")
        self.store.create_branch("feature", fork_event_seq=before_fork.seq)
        after_fork_main = self._event("main-only knowledge")
        cutoff = self.store.known_at_cutoff_seq(
            after_fork_main.created_at, branch_id="feature"
        )
        self.assertEqual(cutoff, before_fork.seq)

    def test_known_at_requires_exact_instant(self) -> None:
        with self.assertRaises(TemporalValidationError):
            self.store.known_at_cutoff_seq("2026-07-13")

    def test_legacy_calls_stay_byte_compatible(self) -> None:
        source = self._event("compat fact evidence")
        record = self._derive("compat fact", source)
        hits = self.service.search(query="compat fact", include_events=False, limit=5)
        target = next(hit for hit in hits if hit.id == record.id)
        self.assertNotIn("validity_status", target.metadata)
        self.assertNotIn("temporal", target.metadata)
        self.assertNotIn("temporal_projection_code_version", target.metadata)
        self.assertIsNone(record.valid_from)

    def test_resume_output_unchanged_without_temporal_options(self) -> None:
        source = self._event("Goal: keep resume stable")
        self._derive("resume stability fact", source, valid_from="2020", valid_to="2021")
        packet = self.service.resume(token_budget=1500)
        self.assertNotIn("validity_status", packet.text)
        self.assertNotIn("[expired]", packet.text)

    def test_snapshot_state_carries_temporal_fields_and_verifies(self) -> None:
        source = self._event("snapshot temporal")
        record = self._derive("snapshotted fact", source, valid_from="2026-02")
        snapshot = self.service.create_snapshot()
        stored = self.store.get_snapshot(snapshot.id)
        entry = stored.state["memories"][record.id]
        self.assertEqual(entry["valid_from"], record.valid_from)
        self.assertEqual(entry["valid_from_precision"], "month")
        self.assertEqual(
            stored.replay_code_version, storage_module.SNAPSHOT_REPLAY_CODE_VERSION
        )

    def test_pre_migration_fixture_database_migrates_and_stays_compatible(self) -> None:
        """task4.md hard rule: a real schema-v7 database (generated by the
        pre-temporal code at commit bf2ec85) migrates additively and legacy
        calls keep their behaviour."""
        import shutil
        import sqlite3
        import uuid as uuid_module

        fixture = Path(__file__).resolve().parent / "fixtures" / "pre_temporal_v7.db"
        working = RUNTIME_ROOT / f"fixture-migration-{uuid_module.uuid4().hex}.db"
        shutil.copyfile(fixture, working)
        service = MemoryService(working, project_root=RUNTIME_ROOT)
        try:
            store = service.store
            version = store._conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()["value"]
            self.assertEqual(version, str(storage_module.CURRENT_SCHEMA_VERSION))
            self.assertEqual(store.verify_chain(), (True, None))
            records = store.list_memories()
            self.assertTrue(records)
            for record in records:
                self.assertIsNone(record.valid_from)
                self.assertIsNone(record.valid_to)
            hits = service.search(query="fixture fact", include_events=False, limit=5)
            self.assertTrue(hits)
            for hit in hits:
                self.assertNotIn("validity_status", hit.metadata)
                self.assertNotIn("temporal", hit.metadata)
            # The pre-migration snapshot stays readable under its recorded
            # replay version; resume still assembles a packet from it.
            packet = service.resume(token_budget=1500)
            self.assertGreater(packet.estimated_tokens, 0)
            migrations = store._conn.execute(
                "SELECT version, from_version FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(
                [(row["version"], row["from_version"]) for row in migrations][-1],
                (storage_module.CURRENT_SCHEMA_VERSION,
                 storage_module.CURRENT_SCHEMA_VERSION - 1),
            )
        finally:
            service.close()
            with sqlite3.connect(fixture) as check:
                stored = check.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertEqual(stored, "7", "fixture itself must never be migrated")

    def test_capabilities_report_bitemporal_support(self) -> None:
        features = self.service.capabilities()["core"]["bitemporal_retrieval"]
        self.assertTrue(features["valid_time_fields"])
        self.assertIn("known_at", features["controls"])
        self.assertEqual(
            features["temporal_projection_code_version"],
            TEMPORAL_PROJECTION_CODE_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
