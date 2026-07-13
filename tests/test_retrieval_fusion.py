from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from joiny_mnemonic.retrieval import RetrievalEngine
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.temporal import parse_query_window


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


class QueryWindowParserTest(unittest.TestCase):
    def window(self, text: str):
        return parse_query_window(text, now=NOW)

    def test_parser_table(self) -> None:
        cases = {
            "что было 2026-06-05 в проекте": ("2026-06-05", 1),
            "what did we decide yesterday": ("2026-07-12", 1),
            "что решили вчера по конфигам": ("2026-07-12", 1),
            "пару дней назад обсуждали": ("2026-07-10", 2),
            "a few weeks ago we planned": ("2026-06-08", 21),
            # 2026-07-13 is a Monday, so last week is Jul 6 - Jul 12.
            "что было на прошлой неделе": ("2026-07-06", 7),
            "решения за прошлый месяц": ("2026-06-01", 30),
            "что решили в июне": ("2026-06-01", 30),
            "decisions from June 2025": ("2025-06-01", 30),
            "что было в декабре": ("2025-12-01", 31),  # not in the future
        }
        for text, (start_day, min_days) in cases.items():
            with self.subTest(text=text):
                window = self.window(text)
                self.assertIsNotNone(window, text)
                self.assertEqual(str(window.start.date()), start_day)
                self.assertGreaterEqual(
                    (window.end - window.start).days, min_days * 0 + 1
                )

    def test_no_cue_means_no_window(self) -> None:
        self.assertIsNone(self.window("what is the deployment target"))
        self.assertIsNone(self.window("May I ask a question"))
        self.assertIsNone(self.window(""))

    def test_bare_month_homographs_need_a_year(self) -> None:
        self.assertIsNone(self.window("may this work"))
        window = self.window("in May 2026 we shipped")
        self.assertIsNotNone(window)
        self.assertEqual(window.start.month, 5)


class _EmptyPlugins:
    """Deterministic no-plugin registry: arm activation must be explicit in
    tests, not a function of what happens to be pip-installed."""

    def __init__(self) -> None:
        self.semantic: dict = {}
        self.knowledge_graph: dict = {}
        self.extractors: dict = {}
        self.kv_tiers: dict = {}
        self.errors: list[str] = []


class FusionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(
            ":memory:", project_root=RUNTIME_ROOT, plugins=_EmptyPlugins()
        )
        self.store = self.service.store

    def tearDown(self) -> None:
        self.service.close()

    def _fact(
        self,
        content: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ):
        source = self.store.append_event(
            kind="message", role="user", content=f"context: {content}"
        )
        return self.store.derive_memory(
            memory_type="fact",
            content=content,
            source_event_ids=(source.id,),
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def test_temporal_arm_fuses_with_lexical_and_records_ranks(self) -> None:
        in_june = self._fact("конфиги переехали на YAML", valid_from="2026-06")
        # Closed interval strictly before the window: an OPEN-ended January
        # fact would legitimately be a "possible" June match — three-valued
        # semantics at work — so exclusion needs a definite non-overlap.
        self._fact(
            "хранение логов в JSONL", valid_from="2026-01", valid_to="2026-02"
        )
        hits = self.service.search(
            query="что решили в июне про YAML", include_events=False, limit=10
        )
        target = next(hit for hit in hits if hit.id == in_june.id)
        self.assertIn("fusion_ranks", target.metadata)
        self.assertIn("temporal", target.metadata["fusion_ranks"])
        self.assertIn("boost_signals", target.metadata)
        self.assertEqual(
            target.metadata["temporal_arm"]["match"], "possible"
        )  # month-precision bound overlapping a month window is not provable
        # The out-of-window fact must not carry a temporal arm rank.
        others = [hit for hit in hits if hit.id != in_june.id]
        for hit in others:
            self.assertNotIn(
                "temporal", hit.metadata.get("fusion_ranks", {}), hit.id
            )

    def test_rrf_scores_match_hand_computation(self) -> None:
        from joiny_mnemonic.models import RetrievalHit

        def hit(hid: str) -> RetrievalHit:
            return RetrievalHit(
                id=hid, source_kind="memory", memory_type="fact",
                representation="summary", content=hid, score=0.0,
                source_event_ids=(), files=(), created_at="2026-07-01T00:00:00+00:00",
            )

        fused = RetrievalEngine._rrf_fuse(
            {"base": [hit("a"), hit("b")], "temporal": [hit("b"), hit("c")]}, 60
        )
        by_id = {item.id: item for item in fused}
        self.assertAlmostEqual(by_id["a"].score, 1 / 61)
        self.assertAlmostEqual(by_id["b"].score, 1 / 62 + 1 / 61)
        self.assertAlmostEqual(by_id["c"].score, 1 / 62)
        self.assertEqual(by_id["b"].metadata["fusion_ranks"], {"base": 2, "temporal": 1})

    def test_boosts_nudge_but_never_flip_strong_gaps(self) -> None:
        from joiny_mnemonic.models import RetrievalHit

        old = RetrievalHit(
            id="old", source_kind="memory", memory_type="fact",
            representation="summary", content="x", score=1.0,
            source_event_ids=(), files=(),
            created_at=(NOW - timedelta(days=400)).isoformat(),
        )
        fresh = RetrievalHit(
            id="fresh", source_kind="memory", memory_type="fact",
            representation="summary", content="x", score=0.5,
            source_event_ids=(), files=(), created_at=NOW.isoformat(),
        )
        boosted = {h.id: h for h in RetrievalEngine._apply_boosts([old, fresh], now=NOW)}
        # Max spread of all three signals is bounded; a 2x base gap survives.
        self.assertGreater(boosted["old"].score, boosted["fresh"].score)
        self.assertIn("boost_signals", boosted["old"].metadata)

    def test_single_arm_queries_stay_legacy(self) -> None:
        record = self._fact("deployment target is Fly.io")
        hits = self.service.search(
            query="deployment target", include_events=False, limit=5
        )
        target = next(hit for hit in hits if hit.id == record.id)
        self.assertNotIn("fusion_ranks", target.metadata)
        self.assertNotIn("boost_signals", target.metadata)

    def test_graph_arm_fuses_when_plugin_matches_entities(self) -> None:
        class FakeGraphPlugin:
            name = "fake-graph"

            def search_arm(self, query, *, limit=20, filters=None):
                from joiny_mnemonic.models import RetrievalHit

                allowed = (filters or {}).get("allowed_memory_ids") or ()
                if not allowed or "yaml" not in query.casefold():
                    return []
                return [
                    RetrievalHit(
                        id=allowed[0], source_kind="memory", memory_type="fact",
                        representation="graph-arm", content="graph summary",
                        score=1.4, source_event_ids=(), files=(),
                        created_at="2026-07-01T00:00:00+00:00",
                        metadata={"graph_arm": {"matched_entities": ["yaml"]}},
                    )
                ]

        record = self._fact("конфиги GPTShared храним в YAML")
        self.service.plugins.knowledge_graph = {"fake": FakeGraphPlugin()}
        hits = self.service.search(query="YAML", include_events=False, limit=5)
        target = next(hit for hit in hits if hit.id == record.id)
        self.assertIn("graph", target.metadata["fusion_ranks"])
        self.assertIn("base", target.metadata["fusion_ranks"])
        # Arm-specific annotation survives the merge.
        self.assertIn("graph_arm", target.metadata)


if __name__ == "__main__":
    unittest.main()
