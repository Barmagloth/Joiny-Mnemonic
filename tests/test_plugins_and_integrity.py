from __future__ import annotations

import importlib.util
import sys
import unittest
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from joiny_mnemonic.mcp import MCPServer, PROTOCOL_VERSION
from joiny_mnemonic.plugins import PluginRegistry
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.storage import MemoryStore, StoreIntegrityError


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


def load_plugin_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


semantic_module = load_plugin_module(
    "joiny_mnemonic_semantic_local_test",
    ROOT
    / "plugins"
    / "semantic-local"
    / "src"
    / "joiny_mnemonic_semantic_local"
    / "__init__.py",
)
graph_module = load_plugin_module(
    "joiny_mnemonic_knowledge_graph_test",
    ROOT
    / "plugins"
    / "knowledge-graph"
    / "src"
    / "joiny_mnemonic_knowledge_graph"
    / "__init__.py",
)
SQLiteKnowledgeGraph = graph_module.SQLiteKnowledgeGraph
LocalSemanticRetriever = semantic_module.LocalSemanticRetriever

def runtime_database(stem: str) -> Path:
    return RUNTIME_ROOT / f"{stem}-{uuid.uuid4().hex}.sqlite"


class SynonymEncoder:
    """Deterministic encoder used to test semantic routing without model downloads."""

    def encode(self, texts: list[str] | tuple[str, ...]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            folded = text.casefold()
            if any(
                term in folded
                for term in (
                    "automobile",
                    "car",
                    "regenerative",
                    "braking",
                    "energy recovery",
                )
            ):
                vectors.append([1.0, 0.0, 0.0])
            elif "database" in folded or "postgres" in folded:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class EncoderLifecycleTest(unittest.TestCase):
    def test_closed_hub_client_is_reset_and_model_load_retried_once(self) -> None:
        sentence_transformers = ModuleType("sentence_transformers")
        huggingface_hub = ModuleType("huggingface_hub")
        attempts: list[str] = []
        resets: list[bool] = []

        class FakeSentenceTransformer:
            def __init__(
                self, model_name: str, *, local_files_only: bool = False
            ) -> None:
                attempts.append(f"{model_name}:{local_files_only}")
                if local_files_only:
                    raise OSError("model is not cached")
                if len(attempts) == 2:
                    raise RuntimeError(
                        "Cannot send a request, as the client has been closed."
                    )

            def encode(self, texts, *, normalize_embeddings):
                self.normalized = normalize_embeddings
                return [[1.0, 0.0] for _ in texts]

        sentence_transformers.SentenceTransformer = FakeSentenceTransformer
        huggingface_hub.close_session = lambda: resets.append(True)
        with patch.dict(
            sys.modules,
            {
                "sentence_transformers": sentence_transformers,
                "huggingface_hub": huggingface_hub,
            },
        ):
            encoder = semantic_module.SentenceTransformerEncoder("test-model")
            vectors = encoder.encode(("first", "second"))

        self.assertEqual(vectors, [[1.0, 0.0], [1.0, 0.0]])
        self.assertEqual(
            attempts,
            [
                "test-model:True",
                "test-model:False",
                "test-model:True",
                "test-model:False",
            ],
        )
        self.assertEqual(resets, [True])

class PluginBehaviorTest(unittest.TestCase):
    def test_semantic_plugin_finds_unmarked_event_without_keyword_overlap(self) -> None:
        registry = PluginRegistry(load_installed=False)
        registry.register_semantic(
            LocalSemanticRetriever(
                runtime_database("semantic"),
                encoder=SynonymEncoder(),
            )
        )
        service = MemoryService(":memory:", project_root=RUNTIME_ROOT, plugins=registry)
        try:
            target = service.store.append_event(
                kind="message",
                role="user",
                content="The automobile uses regenerative braking.",
            )
            service.store.append_event(
                kind="message",
                content="The database migration uses PostgreSQL.",
            )
            hits = service.search(
                query="car energy recovery",
                include_events=True,
                semantic=True,
                limit=5,
            )
            match = next(hit for hit in hits if hit.id == target.id)
            self.assertEqual(match.representation, "semantic")
            self.assertEqual(
                match.metadata["retrieval_backend"],
                "sentence-transformers-cosine",
            )
            self.assertEqual(match.source_event_ids, (target.id,))
        finally:
            service.close()

    def test_knowledge_graph_is_queryable_and_branch_scoped(self) -> None:
        registry = PluginRegistry(load_installed=False)
        registry.register_knowledge_graph(
            SQLiteKnowledgeGraph(runtime_database("knowledge-graph"))
        )
        service = MemoryService(":memory:", project_root=RUNTIME_ROOT, plugins=registry)
        try:
            source = service.store.append_event(
                kind="message",
                content="Joiny-Mnemonic stores canonical history in SQLite.",
            )
            record = service.derive_memory(
                memory_type="decision",
                content="[[Joiny-Mnemonic]] -[stores]-> [[SQLite]]",
                summary="Canonical store",
                source_event_ids=[source.id],
            )
            hits = service.knowledge_neighbors("SQLite")
            edge = next(hit for hit in hits if hit.metadata["memory_id"] == record.id)
            self.assertEqual(edge.metadata["relation"], "stores")
            self.assertEqual(edge.source_event_ids, (source.id,))
            self.assertEqual(service.exact_source(edge.id), [source])
            edge_context = service.context_around(
                edge.id, before=0, after=0, include_source=True
            )
            self.assertEqual(edge_context.source_event_ids, (source.id,))
            self.assertEqual(edge_context.events, (source,))

            service.store.create_branch("child")
            child_record = service.derive_memory(
                memory_type="decision",
                content="[[ChildNode]] -[uses]-> [[SQLite]]",
                source_event_ids=[source.id],
                branch_id="child",
            )
            child_edge = next(
                hit
                for hit in service.knowledge_neighbors("ChildNode", branch_id="child")
                if hit.metadata["memory_id"] == child_record.id
            )
            child_context = service.context_around(
                child_edge.id, before=0, after=0, include_source=True
            )
            self.assertEqual(child_context.branch_id, "child")
            self.assertEqual(child_context.events, (source,))

            hidden_source = service.store.append_event(
                kind="message",
                content="A parent-only graph decision after the fork.",
            )
            hidden = service.derive_memory(
                memory_type="decision",
                content="[[ParentOnly]] -[uses]-> [[HiddenNode]]",
                source_event_ids=[hidden_source.id],
            )
            main_ids = {
                hit.metadata["memory_id"]
                for hit in service.knowledge_neighbors("HiddenNode")
            }
            child_ids = {
                hit.metadata["memory_id"]
                for hit in service.knowledge_neighbors("HiddenNode", branch_id="child")
            }
            self.assertIn(hidden.id, main_ids)
            self.assertNotIn(hidden.id, child_ids)
        finally:
            service.close()


class AutomaticIntegrityTest(unittest.TestCase):
    @staticmethod
    def _tamper(store: MemoryStore, event_id: str) -> None:
        store._conn.execute("DROP TRIGGER IF EXISTS events_no_update")
        store._conn.execute(
            "UPDATE events SET content='tampered outside Joiny-Mnemonic' WHERE id=?",
            (event_id,),
        )

    def test_external_tamper_blocks_reads_and_reopen(self) -> None:
        database = runtime_database("integrity-read")
        store = MemoryStore(database)
        event = store.append_event(kind="message", content="canonical")
        self._tamper(store, event.id)
        self.assertEqual(store.verify_chain()[0], False)
        with self.assertRaises(StoreIntegrityError):
            store.get_event(event.id)
        store.close()
        with self.assertRaises(StoreIntegrityError):
            MemoryStore(database)

    def test_mcp_fails_closed_after_external_tamper(self) -> None:
        database = runtime_database("integrity-mcp")
        service = MemoryService(database, project_root=RUNTIME_ROOT)
        try:
            event = service.store.append_event(kind="message", content="canonical")
            server = MCPServer(service)
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION},
                }
            )
            server.handle(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            self._tamper(service.store, event.id)
            result = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "memory_append",
                        "arguments": {"kind": "message", "content": "must not append"},
                    },
                }
            )
            self.assertTrue(result["result"]["isError"])
            self.assertIn(
                "StoreIntegrityError",
                result["result"]["content"][0]["text"],
            )
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()