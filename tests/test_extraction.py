from __future__ import annotations

import unittest
from pathlib import Path

from joiny_mnemonic.extraction import (
    ExtractorConfig,
    ExtractionValidationError,
    locate_evidence,
)
from joiny_mnemonic.plugins import PluginRegistry
from joiny_mnemonic.service import MemoryService


class FakeExtractor:
    name = "fake"
    model_identity = "deterministic-fake"
    model_version = "1"
    inference_parameters = {"temperature": 0}

    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.events = []

    def extract(self, event, *, context, config):
        self.events.append((event, context, config))
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def config(**values) -> ExtractorConfig:
    defaults = {
        "model_identity": "deterministic-fake",
        "model_version": "1",
        "inference_parameters": {"temperature": 0},
        "auto_threshold": 0.8,
    }
    defaults.update(values)
    return ExtractorConfig(**defaults)


def output(memory_type: str, content: str, quote: str, confidence: float = 0.95):
    return {
        "candidates": [
            {
                "memory_type": memory_type,
                "normalized_content": content,
                "evidence_quote": quote,
                "confidence": confidence,
            }
        ]
    }


class ExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.database = ":memory:"

    def service(self, fake: FakeExtractor, *, enabled: bool = True, cfg=None):
        plugins = PluginRegistry(load_installed=False)
        plugins.register_extractor(fake)
        return MemoryService(
            self.database,
            project_root=self.root,
            plugins=plugins,
            extractor_config=cfg or config(),
            extractor_enabled=enabled,
        )

    def test_config_hash_covers_versions_and_policy(self) -> None:
        original = config()
        self.assertNotEqual(original.canonical_hash, config(model_version="2").canonical_hash)
        self.assertNotEqual(
            original.canonical_hash,
            config(parser_version="exact-markdown-zones-v2").canonical_hash,
        )
        self.assertNotEqual(original.canonical_hash, config(auto_threshold=0.9).canonical_hash)

    def test_exact_prose_creates_complete_atomic_lineage(self) -> None:
        fake = FakeExtractor([
            output("decision", "Использовать SQLite.", "Использовать SQLite.")
        ])
        with self.service(fake) as service:
            event = service.append_event(
                kind="message",
                role="user",
                content="Решили: Использовать SQLite. <private>token=secret</private>",
            )
            self.assertNotIn("secret", fake.events[0][0].content)
            candidate = service.store.list_extraction_candidates()[0]
            self.assertEqual(candidate.current_status, "auto")
            self.assertEqual(
                event.content[candidate.evidence_start:candidate.evidence_end],
                candidate.evidence_quote,
            )
            auto = [
                record for record in service.store.list_memories()
                if record.metadata.get("origin") == "auto"
            ]
            self.assertEqual(len(auto), 1)
            self.assertEqual(auto[0].source_event_ids, (event.id,))
            self.assertEqual(auto[0].metadata["candidate_id"], candidate.id)
            with service.store._lock:
                link = service.store._conn.execute(
                    "SELECT relation FROM candidate_memory_links WHERE candidate_id=?",
                    (candidate.id,),
                ).fetchone()
                attempt = service.store._conn.execute(
                    "SELECT outcome, raw_response_ref FROM extraction_attempts"
                ).fetchone()
            self.assertEqual(link["relation"], "derived")
            self.assertEqual(attempt["outcome"], "succeeded")
            self.assertTrue(attempt["raw_response_ref"])

    def test_quarantine_and_exact_evidence_rejection(self) -> None:
        fake = FakeExtractor([
            output("fact", "Используем PostgreSQL.", "PostgreSQL", 0.99),
            output("fact", "Повтор.", "Повтор"),
            output("fact", "Выдумка.", "нет такой цитаты"),
        ])
        with self.service(fake) as service:
            tick = chr(96)
            service.append_event(
                kind="message", role="user",
                content="Пример: " + tick + "PostgreSQL" + tick,
            )
            service.append_event(
                kind="message", role="user", content="Повтор и ещё Повтор."
            )
            service.append_event(
                kind="message", role="assistant", content="Обычный текст."
            )
            quarantined = service.store.list_extraction_candidates(status="quarantined")
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].evidence_zone, "inline_code")
            with service.store._lock:
                errors = service.store._conn.execute(
                    "SELECT error_code FROM extraction_rejections ORDER BY rowid"
                ).fetchall()
            self.assertEqual(
                [row["error_code"] for row in errors],
                ["ambiguous_evidence", "evidence_not_found"],
            )
            self.assertFalse([
                record for record in service.store.list_memories()
                if record.metadata.get("origin") == "auto"
            ])

    def test_retry_reprocessing_dedup_and_marker_confirmation(self) -> None:
        fake = FakeExtractor([
            RuntimeError("temporary token=secret"),
            output("decision", "Хранить данные в SQLite.", "Хранить данные в SQLite."),
            output("decision", "Хранить данные в SQLite.", "Хранить данные в SQLite."),
            output("decision", "Хранить данные в SQLite.", "Хранить данные в SQLite."),
            output("decision", "Хранить данные в SQLite.", "Хранить данные в SQLite."),
        ])
        with self.service(fake) as service:
            service.append_event(
                kind="message", role="assistant",
                content="Хранить данные в SQLite.",
            )
            self.assertEqual(service.extraction.status().pending_events, 1)
            service.extraction.process_backlog()
            service.append_event(
                kind="message", role="user",
                content="Хранить данные в SQLite.",
            )
            auto = [
                item for item in service.store.list_memories()
                if item.metadata.get("origin") == "auto"
            ]
            self.assertEqual(len(auto), 1)
            service.append_event(
                kind="message", role="user",
                content="Decision: Хранить данные в SQLite.",
            )
            self.assertEqual(service.store.memory_authority(auto[0].id), "confirmed")
            self.assertIn(
                "Хранить данные в SQLite.",
                service.store.get_active_blocks()["decisions"].content,
            )
            old_hash = service.extraction.config_hash
            service.extraction.reprocess(config(model_version="2"))
            self.assertNotEqual(old_hash, service.extraction.config_hash)
            with service.store._lock:
                attempts = service.store._conn.execute(
                    "SELECT outcome, redacted_error FROM extraction_attempts ORDER BY rowid"
                ).fetchall()
            self.assertEqual(attempts[0]["outcome"], "retryable_failure")
            self.assertNotIn("secret", attempts[0]["redacted_error"])

    def test_untrusted_request_and_disabled_compatibility(self) -> None:
        fake = FakeExtractor([
            output("fact", "Проверяем запрос.", "Проверяем запрос.")
        ])
        with self.service(fake) as service:
            service.append_event(
                kind="message", role="assistant", content="Проверяем запрос."
            )
            candidate = service.store.list_extraction_candidates()[0]
            result = service.request_candidate_transition(candidate.id, "confirm")
            self.assertEqual(
                service.store.list_extraction_candidates()[0].current_status,
                "confirmation_requested",
            )
            with self.assertRaises(PermissionError):
                service.store.transition_candidate(
                    candidate.id,
                    "confirmed",
                    source_event_id=result["event_id"],
                    actor="tool",
                    rule_id="forbidden",
                    origin_evidence_type="extractor",
                )

        disabled_db = ":memory:"
        fake = FakeExtractor([
            output("fact", "Не должно запускаться.", "Не должно запускаться.")
        ])
        plugins = PluginRegistry(load_installed=False)
        plugins.register_extractor(fake)
        with MemoryService(
            disabled_db,
            project_root=self.root,
            plugins=plugins,
            extractor_config=config(),
            extractor_enabled=False,
        ) as service:
            event = service.append_event(
                kind="message", role="user", content="Fact: Явный факт."
            )
            self.assertEqual(fake.events, [])
            self.assertEqual(service.store.get_event(event.id).content, "Fact: Явный факт.")
            records = service.store.list_memories(memory_types=("fact",))
            self.assertEqual([item.content for item in records], ["Явный факт."])
            self.assertEqual(service.extraction.status().pending_events, 1)

    def test_quote_locator_marks_fences_and_blockquotes(self) -> None:
        fence = chr(96) * 3
        text = "Проза.\n" + fence + "text\nкод\n" + fence + "\n> цитата\n"
        self.assertEqual(locate_evidence(text, "Проза.")[2], "prose")
        self.assertEqual(locate_evidence(text, "код")[2], "fenced_code")
        self.assertEqual(locate_evidence(text, "цитата")[2], "blockquote")
        with self.assertRaises(ExtractionValidationError):
            locate_evidence("x x", "x")


if __name__ == "__main__":
    unittest.main()