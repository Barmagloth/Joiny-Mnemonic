from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic.cli import build_parser
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class OvercompressionFeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.store = self.service.store

    def tearDown(self) -> None:
        self.service.close()

    def test_signals_column_matches_dates_and_basenames(self) -> None:
        source = self.store.append_event(
            kind="message", role="user", content="context for the record"
        )
        record = self.store.derive_memory(
            memory_type="fact",
            content="конфигурация переехала",
            source_event_ids=(source.id,),
            valid_from="2026-06",
        )
        # BM25 matches the humanized valid-time date although the words
        # appear nowhere in the displayed content (task5.md B4).
        for query in ("June 2026", "июня"):
            hits = self.service.search(
                query=query, include_events=False, limit=5, record_telemetry=False
            )
            self.assertIn(record.id, {hit.id for hit in hits}, query)

    def test_promotions_feed_the_overcompression_report(self) -> None:
        lines = "\n".join(f"log line {index}" for index in range(300))
        event = self.store.append_event(
            kind="tool_output", content=lines,
            payload={"tool_name": "Bash", "tool_input": {"command": "analyze --all"}},
        )
        self.service.reduce_tool_output(event)
        for _ in range(2):
            self.service.exact_source(event.id)
        report = self.service.usage.overcompression_report()
        generic = report["families"]["generic"]
        self.assertEqual(generic["reduced_views"], 1)
        self.assertEqual(generic["source_promotions"], 2)
        # ratio > 0.2 but fewer than 5 views: no recommendation yet.
        self.assertFalse(generic["over_compressed"])
        self.assertEqual(report["recommendation"], [])


class ReportSigningTest(unittest.TestCase):
    def test_stamp_and_verify_roundtrip_detects_tampering(self) -> None:
        import tempfile

        from joiny_mnemonic.report_signing import stamp_report, verify_report

        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "rows.jsonl"
            artifact.write_text('{"row": 1}\n', encoding="utf-8")
            report = stamp_report(
                {"overall": {"accuracy": 0.5}}, artifacts={"rows": artifact}
            )
            self.assertIn("report_sha256", report)
            self.assertEqual(
                len(report["provenance"]["artifact_sha256"]["rows"]), 64
            )
            ok, problems = verify_report(report, artifacts={"rows": artifact})
            self.assertTrue(ok, problems)
            # Silent edits to the summary or the backing rows are detected.
            tampered = {**report, "overall": {"accuracy": 0.99}}
            self.assertFalse(verify_report(tampered)[0])
            artifact.write_text('{"row": 2}\n', encoding="utf-8")
            ok, problems = verify_report(report, artifacts={"rows": artifact})
            self.assertFalse(ok)
            self.assertIn("artifact", problems[0])

    def test_canonical_json_is_order_independent(self) -> None:
        from joiny_mnemonic.report_signing import canonical_json

        one = canonical_json({"b": 1, "a": {"д": "я"}})
        two = canonical_json({"a": {"д": "я"}, "b": 1})
        self.assertEqual(one, two)


class SetupMcpDefaultTest(unittest.TestCase):
    def _resolved_with_mcp(self, argv: list[str]) -> bool:
        from joiny_mnemonic import cli as cli_module

        args = build_parser().parse_args(argv)
        captured: dict = {}

        def fake_run_setup(*_args, **kwargs):
            captured.update(kwargs)
            return {}

        with (
            patch.object(cli_module, "run_setup", side_effect=fake_run_setup),
            patch.object(cli_module, "detect_agents", return_value=()),
        ):
            cli_module.run(args)
        return captured["install_mcp"]

    def test_yes_defaults_to_mcp_on(self) -> None:
        root = RUNTIME_ROOT / "mcp-default"
        root.mkdir(exist_ok=True)
        base = ["--project-root", str(root), "setup", "--yes", "--agent", "codex"]
        self.assertTrue(self._resolved_with_mcp(base))
        self.assertFalse(self._resolved_with_mcp([*base, "--without-mcp"]))
        self.assertTrue(self._resolved_with_mcp([*base, "--with-mcp"]))


if __name__ == "__main__":
    unittest.main()
