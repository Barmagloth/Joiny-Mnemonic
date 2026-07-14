from __future__ import annotations

import inspect
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import Event, MemoryRecord, RetrievalHit


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Project-local paths supplied to installed plugin factories."""

    project_root: Path
    database_path: Path


@runtime_checkable
class SemanticRetriever(Protocol):
    name: str

    def index(self, record: MemoryRecord) -> None: ...
    def search(self, query: str, *, limit: int, filters: dict[str, Any]) -> list[RetrievalHit]: ...


@runtime_checkable
class KnowledgeGraphProjection(Protocol):
    name: str

    def project(self, record: MemoryRecord) -> None: ...
    def neighbors(
        self, entity: str, *, limit: int, filters: dict[str, Any] | None = None
    ) -> list[RetrievalHit]: ...


@runtime_checkable
class KVTier(Protocol):
    name: str

    def put(self, key: str, value: bytes, metadata: dict[str, Any]) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def delete(self, key: str) -> None: ...


@runtime_checkable
class Extractor(Protocol):
    """Optional stateless structured-memory extractor."""

    name: str

    def extract(
        self,
        event: Event,
        *,
        context: tuple[Event, ...],
        config: dict[str, Any],
    ) -> Any: ...


@runtime_checkable
class Reranker(Protocol):
    """Optional final-stage reranker over retrieval hits (task5 benchmark
    calibration: a local cross-encoder is the single largest precision
    lever below oracle retrieval)."""

    name: str

    def rerank(self, query: str, hits: list[RetrievalHit]) -> list[RetrievalHit]: ...


@dataclass(slots=True)
class PluginRegistry:
    semantic: dict[str, SemanticRetriever]
    knowledge_graph: dict[str, KnowledgeGraphProjection]
    kv_tiers: dict[str, KVTier]
    extractors: dict[str, Extractor]
    rerankers: dict[str, Reranker]
    errors: list[str]
    context: PluginContext | None

    def __init__(
        self, *, load_installed: bool = True, context: PluginContext | None = None
    ) -> None:
        self.semantic = {}
        self.knowledge_graph = {}
        self.kv_tiers = {}
        self.extractors = {}
        self.rerankers = {}
        self.errors = []
        self.context = context
        if load_installed:
            # Load legacy groups first; renamed entry points with the same plugin name win.
            for namespace in ("llm_memory", "joiny_mnemonic"):
                self._load_group(f"{namespace}.semantic", self.semantic)
                self._load_group(f"{namespace}.knowledge_graph", self.knowledge_graph)
                self._load_group(f"{namespace}.kv_tier", self.kv_tiers)
                self._load_group(f"{namespace}.extractor", self.extractors)
                self._load_group(f"{namespace}.reranker", self.rerankers)

    def _instantiate(self, loaded: Any) -> Any:
        if not callable(loaded):
            return loaded
        if self.context is None:
            return loaded()
        try:
            parameters = inspect.signature(loaded).parameters.values()
        except (TypeError, ValueError):
            return loaded()
        accepts_context = any(
            parameter.name == "context"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        return loaded(context=self.context) if accepts_context else loaded()

    def _load_group(self, group: str, target: dict[str, Any]) -> None:
        for point in entry_points(group=group):
            try:
                plugin = self._instantiate(point.load())
                target[plugin.name] = plugin
            except Exception as exc:
                self.errors.append(f"{group}:{point.name}: {exc}")

    def register_semantic(self, plugin: SemanticRetriever) -> None:
        self.semantic[plugin.name] = plugin

    def register_knowledge_graph(self, plugin: KnowledgeGraphProjection) -> None:
        self.knowledge_graph[plugin.name] = plugin

    def register_kv_tier(self, plugin: KVTier) -> None:
        self.kv_tiers[plugin.name] = plugin

    def register_extractor(self, plugin: Extractor) -> None:
        self.extractors[plugin.name] = plugin
