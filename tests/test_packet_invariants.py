"""Packet-assembly invariants.

The CHECK MATERIAL probe (benchmarks/results/check-material.md) paid for
this rule empirically: a fixed 600-token reserve for a section that never
fired displaced real evidence and produced the entire measured preference
regression. The invariant, now general: **an optional packet section must
cost zero budget and zero text when it has nothing to say** — no empty
headers, no reserved space, no placeholder lines.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class ProductPacketInvariantTest(unittest.TestCase):
    """Resume packets: optional maintenance sections appear only when they
    carry content."""

    def test_empty_optional_sections_are_absent_not_blank(self) -> None:
        service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        try:
            service.initialize_project()
            packet = service.resume(token_budget=1500)
            # No pending candidates, no recent auto-closures, no stale
            # findings: none of the optional section headers may appear,
            # not even as empty scaffolding.
            for header in (
                "PENDING CONFIRMATIONS",
                "AUTO-CLOSED RECENTLY",
            ):
                self.assertNotIn(header, packet.text, header)
            # And no orphaned section brackets with nothing under them:
            # every "[STATE MAINTENANCE" header must be followed by at
            # least one content line before the next blank line.
            lines = packet.text.splitlines()
            for index, line in enumerate(lines):
                if line.startswith("[STATE MAINTENANCE"):
                    following = lines[index + 1] if index + 1 < len(lines) else ""
                    self.assertTrue(
                        following.strip(),
                        f"empty section scaffolding: {line!r}",
                    )
        finally:
            service.close()

    def test_populated_section_disappears_after_settlement(self) -> None:
        """The counterpart direction: a section that HAD content must fully
        vanish once its source empties (no leftover header)."""
        service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        try:
            service.initialize_project()
            events, _ = service.store.append_host_events_once(
                "receipt:invariant-q",
                [
                    {
                        "kind": "message", "role": "user",
                        "content": "DECISION: перейти на новую схему?",
                        "payload": {},
                    }
                ],
                adapter="claude-code",
            )
            service.consolidator.consolidate_event(service, events[0])
            candidate_id = service.store.list_settlement_candidates(
                kind="block_change", status="pending"
            )[0]["id"]
            with_section = service.resume(token_budget=1500)
            self.assertIn("PENDING CONFIRMATIONS", with_section.text)
            service.settlement.settle(
                candidate_id, "contested",
                reason="инвариант-тест", requested_by="operator",
            )
            after = service.resume(token_budget=1500)
            self.assertNotIn("PENDING CONFIRMATIONS", after.text)
        finally:
            service.close()


class HarnessPacketInvariantTest(unittest.TestCase):
    """Benchmark harness: an armed-but-empty CHECK MATERIAL section must
    produce byte-identical context to the flag being off."""

    def test_empty_check_material_costs_zero(self) -> None:
        from joiny_mnemonic.longmemeval import (
            LMEHarness, LMEQuestion, SubprocessLLMRunner,
        )

        item = LMEQuestion(
            question_id="inv", question_type="x", question="hello?",
            answer="x", question_date="2023-06-10",
            sessions=(
                {
                    "session_id": "s1",
                    "date": "2023/05/23 (Tue) 10:00",
                    "turns": [{"role": "user", "content": "hello world"}],
                },
            ),
        )
        contexts = []
        for flag in (False, True):
            harness = LMEHarness(
                SubprocessLLMRunner(["python", "-c", "pass"]),
                ingest_mode="raw", check_material=flag,
                context_budget_tokens=4096,
            )
            service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
            try:
                harness.ingest(service, item)
                context, _, metrics = harness.build_context(service, item)
            finally:
                service.close()
            contexts.append((context, metrics["context_tokens"]))
        self.assertEqual(contexts[0][0], contexts[1][0])
        self.assertEqual(contexts[0][1], contexts[1][1])


if __name__ == "__main__":
    unittest.main()
