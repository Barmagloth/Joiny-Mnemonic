from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Sequence

from .models import Event, MemoryRecord, RetrievalHit
from .plugins import PluginRegistry
from .storage import MemoryStore


WORD = re.compile(r"[\w./:-]+", re.UNICODE)


def lexical_terms(value: str) -> set[str]:
    return {item.casefold() for item in WORD.findall(value) if len(item) > 1}


def _lexical_relevance(query: str, content: str) -> float:
    query_terms = lexical_terms(query)
    if not query_terms:
        return 0.5
    content_terms = lexical_terms(content)
    overlap = len(query_terms & content_terms)
    coverage = overlap / len(query_terms)
    precision = overlap / max(len(content_terms), 1)
    phrase = 1.0 if query.casefold() in content.casefold() else 0.0
    return min(1.0, coverage * 0.7 + math.sqrt(precision) * 0.15 + phrase * 0.15)


def _freshness(created_at: str, half_life_days: float) -> float:
    try:
        age = max(0.0, (datetime.now(UTC) - datetime.fromisoformat(created_at)).total_seconds())
    except ValueError:
        return 0.0
    half_life = max(half_life_days, 0.01) * 86400
    return math.exp(-math.log(2) * age / half_life)


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    query: str = ""
    branch_id: str = "main"
    memory_types: tuple[str, ...] = ()
    file: str | None = None
    since: str | None = None
    until: str | None = None
    limit: int = 10
    exact: bool = False
    include_events: bool = True
    semantic: bool = True
    half_life_days: float = 30.0
    relevance_weight: float = 0.65
    freshness_weight: float = 0.10
    risk_weight: float = 0.15
    cost_weight: float = 0.10


class RetrievalEngine:
    def __init__(self, store: MemoryStore, plugins: PluginRegistry | None = None) -> None:
        self.store = store
        self.plugins = plugins or PluginRegistry()

    def _memory_hit(self, record: MemoryRecord, context: RetrievalContext) -> RetrievalHit:
        relevance = _lexical_relevance(
            context.query, f"{record.summary}\n{record.content}\n{' '.join(record.files)}"
        )
        freshness = _freshness(record.created_at, context.half_life_days)
        cost_efficiency = 1.0 / (1.0 + record.retrieval_cost)
        total_weight = max(
            context.relevance_weight + context.freshness_weight
            + context.risk_weight + context.cost_weight,
            1e-9,
        )
        score = (
            relevance * context.relevance_weight
            + freshness * context.freshness_weight
            + record.risk * context.risk_weight
            + cost_efficiency * context.cost_weight
        ) / total_weight
        content = record.content if context.exact else record.summary
        return RetrievalHit(
            id=record.id,
            source_kind="memory",
            memory_type=record.memory_type,
            representation="detail" if context.exact else "summary",
            content=content,
            score=score,
            source_event_ids=record.source_event_ids,
            files=record.files,
            created_at=record.created_at,
            metadata={
                "version": record.version,
                "risk": record.risk,
                "retrieval_cost": record.retrieval_cost,
                "scoring_context": {
                    "query": context.query,
                    "half_life_days": context.half_life_days,
                },
            },
        )

    @staticmethod
    def _event_hit(event: Event, context: RetrievalContext) -> RetrievalHit:
        relevance = _lexical_relevance(
            context.query, f"{event.content}\n{' '.join(event.files)}"
        )
        freshness = _freshness(event.created_at, context.half_life_days)
        score = relevance * 0.85 + freshness * 0.15
        return RetrievalHit(
            id=event.id,
            source_kind="event",
            memory_type=event.kind,
            representation="source",
            content=event.content,
            score=score,
            source_event_ids=(event.id,),
            files=event.files,
            created_at=event.created_at,
            metadata={"seq": event.seq, "role": event.role, "payload": event.payload},
        )

    def search(self, context: RetrievalContext) -> list[RetrievalHit]:
        if context.limit < 1:
            return []
        hits: list[RetrievalHit] = []
        if context.query and self.store.fts_enabled:
            memory_candidates = self.store.search_memories_fts(
                context.query,
                branch_id=context.branch_id,
                memory_types=context.memory_types,
                since=context.since,
                until=context.until,
                file=context.file,
                limit=max(context.limit * 4, 20),
            )
            for position, (record, rank) in enumerate(memory_candidates):
                hit = self._memory_hit(record, context)
                metadata = dict(hit.metadata)
                metadata.update({"retrieval_backend": "fts5-bm25", "fts_rank": rank})
                hits.append(
                    replace(
                        hit,
                        score=min(1.0, hit.score + 0.08 / (position + 1)),
                        metadata=metadata,
                    )
                )
            if context.include_events:
                event_candidates = self.store.search_events_fts(
                    context.query,
                    branch_id=context.branch_id,
                    since=context.since,
                    until=context.until,
                    file=context.file,
                    limit=max(context.limit * 4, 20),
                )
                for position, (event, rank) in enumerate(event_candidates):
                    hit = self._event_hit(event, context)
                    metadata = dict(hit.metadata)
                    metadata.update({"retrieval_backend": "fts5-bm25", "fts_rank": rank})
                    hits.append(
                        replace(
                            hit,
                            score=min(1.0, hit.score + 0.08 / (position + 1)),
                            metadata=metadata,
                        )
                    )
        else:
            records = self.store.list_memories(
                branch_id=context.branch_id,
                memory_types=context.memory_types,
                since=context.since,
                until=context.until,
                file=context.file,
            )
            hits.extend(self._memory_hit(record, context) for record in records)
            if context.include_events:
                events = self.store.query_events(
                    branch_id=context.branch_id,
                    since=context.since,
                    until=context.until,
                    text=None,
                    file=context.file,
                )
                hits.extend(self._event_hit(event, context) for event in events)
            if context.query:
                hits = [hit for hit in hits if hit.score > 0.05]

        if context.semantic and context.query and self.plugins.semantic:
            visible_records = self.store.list_memories(
                branch_id=context.branch_id,
                memory_types=context.memory_types,
                since=context.since,
                until=context.until,
                file=context.file,
            )
            visible_events = (
                self.store.query_events(
                    branch_id=context.branch_id,
                    since=context.since,
                    until=context.until,
                    file=context.file,
                )
                if context.include_events else []
            )
            filters: dict[str, Any] = {
                "branch_id": context.branch_id,
                "memory_types": context.memory_types,
                "file": context.file,
                "since": context.since,
                "until": context.until,
                "allowed_memory_ids": tuple(record.id for record in visible_records),
                "allowed_event_ids": tuple(event.id for event in visible_events),
            }
            for plugin in self.plugins.semantic.values():
                try:
                    sync = getattr(plugin, "sync", None)
                    if callable(sync):
                        sync(visible_records, visible_events)
                    else:
                        for record in visible_records:
                            plugin.index(record)
                    hits.extend(
                        plugin.search(context.query, limit=context.limit, filters=filters)
                    )
                except Exception as exc:
                    error = f"semantic:{plugin.name}: {exc}"
                    if error not in self.plugins.errors:
                        self.plugins.errors.append(error)
        deduplicated: dict[tuple[str, str], RetrievalHit] = {}
        for hit in hits:
            key = (hit.source_kind, hit.id)
            if key not in deduplicated or hit.score > deduplicated[key].score:
                deduplicated[key] = hit
        return sorted(
            deduplicated.values(), key=lambda hit: (hit.score, hit.created_at), reverse=True
        )[: context.limit]

    def search_records(
        self,
        context: RetrievalContext,
        records: Sequence[MemoryRecord],
    ) -> list[RetrievalHit]:
        """Rank a materialized snapshot without reading mutable current-store views."""
        selected = [
            record for record in records
            if (not context.memory_types or record.memory_type in context.memory_types)
            and (not context.file or context.file in record.files)
            and (not context.since or record.created_at >= context.since)
            and (not context.until or record.created_at <= context.until)
        ]
        hits = [self._memory_hit(record, context) for record in selected]
        if context.query:
            hits = [hit for hit in hits if hit.score > 0.01]
        return sorted(
            hits, key=lambda hit: (hit.score, hit.created_at), reverse=True
        )[: context.limit]

    def promote_to_source(self, hit: RetrievalHit) -> list[RetrievalHit]:
        """Resolve a summary/detail hit to exact immutable source events."""
        events = [self.store.get_event(event_id) for event_id in hit.source_event_ids]
        context = RetrievalContext(query="", exact=True)
        return [self._event_hit(event, context) for event in events]

    def timeline(
        self, *, branch_id: str = "main", limit: int = 50, kinds: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        events = self.store.query_events(branch_id=branch_id, kinds=kinds, limit=limit)
        return [
            {
                "seq": event.seq,
                "id": event.id,
                "time": event.created_at,
                "kind": event.kind,
                "role": event.role,
                "preview": event.content[:160],
                "files": list(event.files),
            }
            for event in events
        ]
