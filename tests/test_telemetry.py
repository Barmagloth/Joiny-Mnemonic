from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic.hooks import process_hook
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"


class RetrievalTelemetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.source = self.service.store.append_event(
            kind="message",
            role="user",
            content="Telemetry source evidence.",
            files=("src/auth.py",),
        )
        self.memory = self.service.derive_memory(
            memory_type="fact",
            content="Telemetry needle uses exact provenance.",
            source_event_ids=(self.source.id,),
            files=("src/auth.py",),
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_search_exposure_records_ids_measurements_filters_and_redaction(self) -> None:
        with patch("joiny_mnemonic.retrieval._freshness", return_value=0.5):
            without = self.service.search(
                query="Telemetry needle",
                include_events=False,
                semantic=False,
                memory_types=("fact",),
                file="src/auth.py",
                limit=7,
                record_telemetry=False,
            )
            with_telemetry = self.service.search(
                query="Telemetry needle",
                include_events=False,
                semantic=False,
                memory_types=("fact",),
                file="src/auth.py",
                limit=7,
            )

        self.assertEqual(
            [asdict(hit) for hit in without],
            [asdict(hit) for hit in with_telemetry],
        )
        sample = self.service.store.list_usage_samples(
            operation="retrieval_search"
        )[0]
        self.assertEqual(sample.operation, "retrieval_search")
        self.assertEqual(sample.metadata["query"], "Telemetry needle")
        self.assertFalse(sample.metadata["semantic_enabled"])
        self.assertEqual(sample.metadata["limit"], 7)
        self.assertEqual(sample.metadata["filters"]["memory_types"], ["fact"])
        self.assertEqual(sample.metadata["filters"]["file"], "src/auth.py")
        result = sample.metadata["results"][0]
        self.assertEqual(result["id"], with_telemetry[0].id)
        self.assertEqual(result["score"], with_telemetry[0].score)
        self.assertEqual(result["source_kind"], "memory")
        self.assertEqual(result["position"], 0)
        self.assertNotIn(self.memory.content, json.dumps(sample.metadata))

        secret = "api_key=telemetry-secret-value"
        self.service.search(
            query=f"Telemetry {secret}",
            include_events=False,
            semantic=False,
        )
        redacted = self.service.store.list_usage_samples(
            operation="retrieval_search"
        )[-1]
        self.assertNotIn(secret, json.dumps(redacted.metadata))
        self.assertIn("[REDACTED]", redacted.metadata["query"])

    def test_prompt_exposure_is_correlated_and_output_is_unchanged(self) -> None:
        task = self.service.tasks.start("TEL-1", "Telemetry task")
        session_id = self.service.store.start_session(
            "test-agent", branch_id=task.branch_id
        )
        query = "Telemetry needle"
        without = self.service.resume(
            branch_id=task.branch_id,
            query=query,
            session_id=session_id,
            task_key=task.task_key,
            record_telemetry=False,
        )
        with_telemetry = self.service.resume(
            branch_id=task.branch_id,
            query=query,
            session_id=session_id,
            task_key=task.task_key,
            telemetry_receipt="prompt:tel-1",
        )
        self.assertEqual(without, with_telemetry)

        sample = self.service.store.list_usage_samples(
            branch_id=task.branch_id,
            operation="prompt_injection",
        )[0]
        self.assertEqual(sample.session_id, session_id)
        self.assertEqual(sample.metadata["task_key"], task.task_key)
        self.assertEqual(
            sample.metadata["included_event_ids"],
            list(with_telemetry.included_event_ids),
        )
        self.assertEqual(
            sample.metadata["included_memory_ids"],
            list(with_telemetry.included_memory_ids),
        )
        self.assertEqual(sample.metadata["snapshot_id"], with_telemetry.snapshot_id)
        self.assertEqual(sample.metadata["token_budget"], with_telemetry.token_budget)
        self.assertEqual(
            sample.metadata["estimated_emitted_tokens"],
            with_telemetry.estimated_tokens,
        )
        approval = self.service.store.append_host_event(
            adapter="codex", branch_id=task.branch_id,
            kind="message", role="user", content="complete task",
        )
        completed = self.service.tasks.complete(
            task.task_key, source_event_id=approval.id
        )
        self.assertEqual(completed.status, "completed")

        report = self.service.usage.report(branch_id=task.branch_id)
        self.assertEqual(report["totals"]["prompt_injection_count"], 1)
        self.assertEqual(report["totals"]["task_correlated_exposure_count"], 1)
        self.assertEqual(
            report["by_operation"]["prompt_injection"]["samples"],
            1,
        )

    def test_hook_retry_deduplicates_prompt_exposure(self) -> None:
        value = {
            "hook_event_name": "SessionStart",
            "session_id": "telemetry-hook-retry",
            "source": "startup",
        }
        first = process_hook(self.service, "claude-code", value)
        second = process_hook(self.service, "claude-code", value)
        self.assertEqual(first, second)

        samples = self.service.store.list_usage_samples(
            operation="prompt_injection"
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].session_id, self.service.store.hook_session(
            "claude-code",
            "telemetry-hook-retry",
            branch_id="main",
        ))

    def test_telemetry_failure_never_fails_search_or_prompt(self) -> None:
        with patch.object(
            self.service.usage,
            "record_retrieval_search",
            side_effect=RuntimeError("telemetry unavailable"),
        ):
            hits = self.service.search(
                query="Telemetry needle",
                include_events=False,
                semantic=False,
            )
        self.assertIn(self.memory.id, {hit.id for hit in hits})

        with patch.object(
            self.service.usage,
            "record_prompt_injection",
            side_effect=RuntimeError("telemetry unavailable"),
        ):
            packet = self.service.prompts.assemble(
                token_budget=600,
                query="Telemetry needle",
            )
        self.assertIn("[MEMORY PACKET]", packet.text)

    def test_usage_aggregation_counts_search_results_and_prompt_ids(self) -> None:
        self.service.search(
            query="Telemetry needle",
            include_events=False,
            semantic=False,
        )
        packet = self.service.resume(query="Telemetry needle")
        report = self.service.usage.report()

        self.assertEqual(report["totals"]["retrieval_search_count"], 1)
        self.assertGreaterEqual(report["totals"]["retrieval_result_count"], 1)
        self.assertEqual(report["totals"]["prompt_injection_count"], 1)
        self.assertEqual(
            report["totals"]["prompt_included_event_count"],
            len(packet.included_event_ids),
        )
        self.assertEqual(
            report["totals"]["prompt_included_memory_count"],
            len(packet.included_memory_ids),
        )


if __name__ == "__main__":
    unittest.main()
