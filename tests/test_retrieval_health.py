"""Channel health/watermark (2026-07-17).

The incident this covers: a live install silently ran lexical-only for
days because an absent semantic plugin produced empty results that were
indistinguishable from healthy ones. Health is a cheap maintained
projection — arms mark success (with the head they synced against) or
failure during normal search; deltas persist across process boundaries.
"""
from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class _EmptyPlugins:
    def __init__(self) -> None:
        self.semantic: dict = {}
        self.knowledge_graph: dict = {}
        self.extractors: dict = {}
        self.kv_tiers: dict = {}
        self.rerankers: dict = {}
        self.errors: list[str] = []


class _BoomPlugin:
    name = "boom"

    def sync(self, records, events=()):
        raise RuntimeError("index corrupted")

    def search(self, query, *, limit, filters):
        return []


class _BoomPlugins(_EmptyPlugins):
    def __init__(self) -> None:
        super().__init__()
        self.semantic = {"boom": _BoomPlugin()}


class RetrievalHealthTest(unittest.TestCase):
    def test_lexical_watermark_and_absent_optional_channels(self) -> None:
        service = MemoryService(
            ":memory:", project_root=RUNTIME_ROOT, plugins=_EmptyPlugins()
        )
        try:
            service.initialize_project()
            event = service.store.append_event(
                kind="message", role="user", content="конфиги храним в YAML"
            )
            service.search(query="конфиги", record_telemetry=False)
            health = service.retrieval_health()
            lexical = health["channels"]["lexical"]
            self.assertTrue(lexical["configured"])
            self.assertFalse(lexical["degraded"])
            self.assertEqual(lexical["indexed_through_seq"], event.seq)
            self.assertEqual(lexical["lag"], 0)
            # The silent-death signal: optional channels reported absent
            # explicitly, not as quietly empty results.
            self.assertTrue(health["absent_optional"]["semantic"])
            self.assertTrue(health["absent_optional"]["reranker"])
            # Watermark lags after new events until the next search.
            service.store.append_event(
                kind="message", role="user", content="ещё событие"
            )
            health = service.retrieval_health()
            self.assertEqual(health["channels"]["lexical"]["lag"], 1)
        finally:
            service.close()

    def test_failing_semantic_arm_is_degraded_and_persisted(self) -> None:
        database = RUNTIME_ROOT / f"health-{uuid.uuid4().hex}.db"
        self.addCleanup(lambda: database.unlink(missing_ok=True))
        service = MemoryService(
            database, project_root=RUNTIME_ROOT, plugins=_BoomPlugins()
        )
        try:
            service.initialize_project()
            service.store.append_event(
                kind="message", role="user", content="что решили по конфигам"
            )
            service.search(query="конфиги", record_telemetry=False)
            health = service.retrieval_health()
            boom = health["channels"]["semantic:boom"]
            self.assertTrue(boom["degraded"])
            self.assertIn("index corrupted", boom["last_error"])
            # The degraded channel surfaces in the resume packet as a
            # staleness disclosure.
            packet = service.resume(token_budget=1500)
            self.assertIn("retrieval channel semantic:boom degraded", packet.text)
        finally:
            service.close()
        # A fresh process (new service over the same store, plugin now
        # absent entirely) still sees the persisted last known state.
        reopened = MemoryService(
            database, project_root=RUNTIME_ROOT, plugins=_EmptyPlugins()
        )
        try:
            health = reopened.retrieval_health()
            boom = health["channels"].get("semantic:boom")
            self.assertIsNotNone(boom)
            self.assertTrue(boom["degraded"])
            self.assertTrue(health["absent_optional"]["semantic"])
        finally:
            reopened.close()

    def test_capabilities_expose_channel_health(self) -> None:
        service = MemoryService(
            ":memory:", project_root=RUNTIME_ROOT, plugins=_EmptyPlugins()
        )
        try:
            service.initialize_project()
            health = service.capabilities()["core"]["retrieval_health"]
            self.assertIn("lexical", health["channels"])
            self.assertIn("absent_optional", health)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
