from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

from .models import MemoryRecord, RetrievalHit


@runtime_checkable
class SemanticRetriever(Protocol):
    name: str

    def index(self, record: MemoryRecord) -> None: ...
    def search(self, query: str, *, limit: int, filters: dict[str, Any]) -> list[RetrievalHit]: ...


@runtime_checkable
class KnowledgeGraphProjection(Protocol):
    name: str

    def project(self, record: MemoryRecord) -> None: ...
    def neighbors(self, entity: str, *, limit: int) -> list[RetrievalHit]: ...


@runtime_checkable
class KVTier(Protocol):
    name: str

    def put(self, key: str, value: bytes, metadata: dict[str, Any]) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def delete(self, key: str) -> None: ...


@dataclass(slots=True)
class PluginRegistry:
    semantic: dict[str, SemanticRetriever]
    knowledge_graph: dict[str, KnowledgeGraphProjection]
    kv_tiers: dict[str, KVTier]
    errors: list[str]

    def __init__(self, *, load_installed: bool = True) -> None:
        self.semantic = {}
        self.knowledge_graph = {}
        self.kv_tiers = {}
        self.errors = []
        if load_installed:
            # Load legacy groups first; renamed entry points with the same plugin name win.
            for namespace in ("llm_memory", "joiny_mnemonic"):
                self._load_group(f"{namespace}.semantic", self.semantic)
                self._load_group(f"{namespace}.knowledge_graph", self.knowledge_graph)
                self._load_group(f"{namespace}.kv_tier", self.kv_tiers)

    def _load_group(self, group: str, target: dict[str, Any]) -> None:
        for point in entry_points(group=group):
            try:
                plugin = point.load()()
                target[plugin.name] = plugin
            except Exception as exc:
                self.errors.append(f"{group}:{point.name}: {exc}")

    def register_semantic(self, plugin: SemanticRetriever) -> None:
        self.semantic[plugin.name] = plugin

    def register_knowledge_graph(self, plugin: KnowledgeGraphProjection) -> None:
        self.knowledge_graph[plugin.name] = plugin

    def register_kv_tier(self, plugin: KVTier) -> None:
        self.kv_tiers[plugin.name] = plugin
