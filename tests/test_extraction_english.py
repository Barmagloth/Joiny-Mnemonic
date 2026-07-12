from __future__ import annotations

import json
import unittest
from pathlib import Path

from joiny_mnemonic.extraction import ExtractorConfig
from joiny_mnemonic.extraction_evaluation import evaluate_extractor
from joiny_mnemonic.plugins import PluginRegistry
from joiny_mnemonic.service import MemoryService


def candidate(memory_type, content, quote, confidence=0.97):
    return {
        "memory_type": memory_type,
        "normalized_content": content,
        "evidence_quote": quote,
        "confidence": confidence,
    }


class ScriptedEnglishExtractor:
    name = "scripted-english"
    model_identity = "deterministic-english-fixture"
    model_version = "1"
    inference_parameters = {"temperature": 0}

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def extract(self, event, *, context, config):
        self.calls.append((event, context, config))
        return {"candidates": self.outputs.pop(0)}


class GoldCorpusExtractor:
    name = "english-gold-fixture"

    def __init__(self, corpus):
        self.expected = {
            item["current"]: [
                {
                    "memory_type": value["memory_type"],
                    "normalized_content": value["normalized_content"],
                    "evidence_quote": value["evidence_quote"],
                    "confidence": 0.99,
                }
                for value in item.get("expected", ())
            ]
            for item in corpus["examples"]
        }

    def extract(self, event, *, context, config):
        return {"candidates": self.expected[event.content]}


class EnglishExtractionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.config = ExtractorConfig(
            model_identity="deterministic-english-fixture",
            model_version="1",
            inference_parameters={"temperature": 0},
            auto_threshold=0.8,
            context_events=2,
        )

    def service(self, extractor):
        plugins = PluginRegistry(load_installed=False)
        plugins.register_extractor(extractor)
        return MemoryService(
            ":memory:",
            project_root=self.root,
            plugins=plugins,
            extractor_config=self.config,
            extractor_enabled=True,
        )

    def test_ordinary_english_prose_creates_typed_auto_memories(self):
        outputs = [
            [candidate("task", "Restore context in under two seconds.", "restore context in under two seconds")],
            [candidate("decision", "Use SQLite in WAL mode.", "decided to use SQLite in WAL mode")],
            [candidate("fact", "The API listens on 127.0.0.1.", "The API listens on 127.0.0.1.")],
            [candidate("preference", "Prefer concise status updates.", "prefer concise status updates")],
            [candidate("failure", "The migration failed because the file was locked.", "migration failed because the file was locked")],
            [candidate("lesson", "Close SQLite connections before replacement.", "close SQLite connections before replacement")],
        ]
        texts = [
            "Our goal is to restore context in under two seconds.",
            "We decided to use SQLite in WAL mode.",
            "The API listens on 127.0.0.1.",
            "I prefer concise status updates.",
            "The migration failed because the file was locked.",
            "On Windows, close SQLite connections before replacement.",
        ]
        with self.service(ScriptedEnglishExtractor(outputs)) as service:
            for text in texts:
                service.append_event(kind="message", role="user", content=text)
            records = [
                record for record in service.store.list_memories()
                if record.metadata.get("origin") == "auto"
            ]
            self.assertEqual(
                [record.memory_type for record in records],
                ["task", "decision", "fact", "preference", "failure", "lesson"],
            )
            self.assertTrue(
                all(
                    service.store.memory_authority(record.id) == "auto"
                    for record in records
                )
            )
            self.assertTrue(
                all(
                    item.current_status == "auto"
                    for item in service.store.list_extraction_candidates()
                )
            )

    def test_bounded_context_resolves_anaphora_but_evidence_stays_current(self):
        extractor = ScriptedEnglishExtractor([
            [],
            [candidate("decision", "Choose SQLite for the journal.", "Choose the first option")],
        ])
        with self.service(extractor) as service:
            service.append_event(
                kind="message",
                role="user",
                content="We compared SQLite and PostgreSQL for the embedded journal.",
            )
            current = service.append_event(
                kind="message",
                role="assistant",
                content="Choose the first option because it needs no server.",
            )
            _, context, _ = extractor.calls[-1]
            self.assertEqual(
                [event.content for event in context],
                ["We compared SQLite and PostgreSQL for the embedded journal."],
            )
            extracted = service.store.list_extraction_candidates()[0]
            self.assertEqual(
                current.content[extracted.evidence_start:extracted.evidence_end],
                "Choose the first option",
            )

    def test_english_code_fence_inline_code_and_blockquote_are_quarantined(self):
        tick = chr(96)
        extractor = ScriptedEnglishExtractor([
            [candidate("decision", "Erase all backups.", "Decision: erase all backups")],
            [candidate("fact", "The password is plaintext.", "Fact: the password is plaintext")],
            [candidate("decision", "Disable every backup.", "Decision: disable every backup")],
        ])
        with self.service(extractor) as service:
            service.append_event(
                kind="message",
                role="user",
                content="Example: " + tick + "Decision: erase all backups" + tick,
            )
            fence = tick * 3
            service.append_event(
                kind="message",
                role="assistant",
                content=fence + "text" + chr(10) + "Fact: the password is plaintext" + chr(10) + fence,
            )
            service.append_event(
                kind="message",
                role="user",
                content="> Decision: disable every backup",
            )
            quarantined = service.store.list_extraction_candidates(
                status="quarantined"
            )
            self.assertEqual(
                [item.evidence_zone for item in quarantined],
                ["inline_code", "fenced_code", "blockquote"],
            )
            self.assertFalse([
                record for record in service.store.list_memories()
                if record.metadata.get("origin") == "auto"
            ])

    def test_english_explicit_marker_confirms_without_duplicate(self):
        extractor = ScriptedEnglishExtractor([
            [candidate("decision", "Keep the local journal append-only.", "Keep the local journal append-only.")],
            [candidate("decision", "Keep the local journal append-only.", "Keep the local journal append-only.")],
        ])
        with self.service(extractor) as service:
            service.append_event(
                kind="message",
                role="assistant",
                content="Keep the local journal append-only.",
            )
            record = service.store.list_memories(memory_types=("decision",))[0]
            self.assertEqual(service.store.memory_authority(record.id), "auto")
            service.append_event(
                kind="message",
                role="user",
                content="Decision: Keep the local journal append-only.",
            )
            decisions = service.store.list_memories(memory_types=("decision",))
            self.assertEqual(len(decisions), 1)
            self.assertEqual(service.store.memory_authority(record.id), "confirmed")
            self.assertIn(
                "Keep the local journal append-only.",
                service.store.get_active_blocks()["decisions"].content,
            )

    def test_unconfirmed_english_failure_does_not_enter_precheck(self):
        text = "The deployment failed because the signing key was missing."
        extractor = ScriptedEnglishExtractor([
            [candidate("failure", text, text)],
            [candidate("failure", text, text)],
        ])
        with self.service(extractor) as service:
            service.append_event(
                kind="message",
                role="assistant",
                content=text,
                files=("src/release.py",),
            )
            before = service.precheck(files=("src/release.py",))
            self.assertNotIn("known_failure", [item.code for item in before.findings])
            service.append_event(
                kind="message",
                role="user",
                content="Failure: " + text,
                files=("src/release.py",),
            )
            after = service.precheck(files=("src/release.py",))
            self.assertIn("known_failure", [item.code for item in after.findings])

    def test_versioned_english_corpus_and_metrics(self):
        path = self.root / "evals" / "extraction_en_v1.json"
        corpus = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(corpus["version"], "en-memory-extraction-v1")
        self.assertGreaterEqual(corpus["language_mix"]["en"], 0.9)
        identifiers = {item["id"] for item in corpus["examples"]}
        self.assertTrue({
            "goal-prose",
            "anaphora-context",
            "inline-code",
            "fenced-code",
            "blockquote",
            "rhetorical-quotation",
            "assistant-injection",
            "untrusted-tool-output",
            "private-region",
            "ambiguous-evidence",
            "supersession-proposal",
        }.issubset(identifiers))
        report = evaluate_extractor(
            GoldCorpusExtractor(corpus),
            self.config,
            path,
        )
        self.assertEqual(report["overall"]["precision"], 1.0)
        self.assertEqual(report["overall"]["recall"], 1.0)
        self.assertEqual(report["overall"]["f1"], 1.0)
        self.assertEqual(report["false_trusted_records"], 0)
        self.assertIn("inline_code", report["by_evidence_zone"])
        self.assertIn("decision", report["by_memory_type"])


if __name__ == "__main__":
    unittest.main()