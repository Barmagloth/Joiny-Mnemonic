from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Sequence

from . import temporal
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
    # Bitemporal controls (task4.md); all default to the legacy
    # transaction-time behaviour and add nothing to hit metadata when unset.
    valid_at: str | None = None
    known_at: str | None = None
    current: bool = False
    include_unknown_validity: bool = False
    history: bool = False

    @property
    def temporal_active(self) -> bool:
        return bool(self.valid_at or self.known_at or self.current or self.history)


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
        origin = str(record.metadata.get("origin", "explicit"))
        authority = self.store.memory_authority(record.id)
        if authority != "confirmed":
            score *= 0.85
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
                "origin": origin,
                "authority_level": authority,
                "candidate_id": record.metadata.get("candidate_id"),
                "extraction_run_id": record.metadata.get("extraction_run_id"),
                "extractor_config_hash": record.metadata.get("extractor_config_hash"),
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

    # --- temporal arm and fusion (task5.md B1-B3; recipe constants from
    # Hindsight, arXiv 2512.12818 / vectorize-io, Apache-2.0) ---------------

    _TEMPORAL_POOL = 60
    _TEMPORAL_KEEP = 10
    _TEMPORAL_BUCKETS = 8
    _RRF_K = 60

    def _temporal_arm(
        self, context: RetrievalContext, window: temporal.QueryWindow
    ) -> list[RetrievalHit]:
        """Candidates whose validity/admission intersects the query window,
        proximity-scored and coverage-selected across time buckets."""
        scored: list[tuple[int, float, datetime, RetrievalHit]] = []

        def consider(hit: RetrievalHit, interval: temporal.Interval,
                     anchor: datetime | None) -> None:
            overlap = temporal.overlaps(interval, window.interval)
            if overlap is temporal.Truth.FALSE:
                return
            definite = 0 if overlap is temporal.Truth.TRUE else 1
            if anchor is None:
                proximity, midpoint = 0.5, window.midpoint
            else:
                half = max((window.end - window.start).total_seconds() / 2, 1.0)
                distance = abs((anchor - window.midpoint).total_seconds())
                proximity = max(0.0, 1.0 - min(distance / half, 1.0))
                midpoint = anchor
            metadata = dict(hit.metadata)
            metadata["temporal_arm"] = {
                "window": window.expression,
                "match": "definite" if definite == 0 else "possible",
                "proximity": round(proximity, 4),
            }
            scored.append(
                (definite, proximity, midpoint,
                 replace(hit, score=proximity, metadata=metadata))
            )

        for record in self.store.list_memories(
            branch_id=context.branch_id,
            memory_types=context.memory_types,
            file=context.file,
        ):
            interval = temporal.interval_from_fields(
                record.valid_from, record.valid_from_precision,
                record.valid_to, record.valid_to_precision,
            )
            anchor: datetime | None
            if interval.start.known or interval.end.known:
                anchor = interval.start.lo or interval.end.lo
            else:
                # No valid-time assertion: fall back to admission time as a
                # point interval (documented simplification, task5.md B1).
                admitted = datetime.fromisoformat(record.created_at)
                interval = temporal.Interval(
                    temporal.Envelope(admitted, admitted, True),
                    temporal.Envelope(admitted, admitted, True),
                )
                anchor = admitted
            consider(self._memory_hit(record, context), interval, anchor)

        if context.include_events:
            for event in self.store.query_events(
                branch_id=context.branch_id,
                since=window.start.isoformat(),
                until=window.end.isoformat(),
            ):
                if event.kind not in ("message", "tool_output"):
                    continue
                admitted = datetime.fromisoformat(event.created_at)
                point = temporal.Envelope(admitted, admitted, True)
                # Containment via the temporal core, not raw comparison
                # (single-source-of-truth invariant; review finding M10).
                if temporal.contains(window.interval, point) is not temporal.Truth.TRUE:
                    continue
                consider(
                    self._event_hit(event, context),
                    temporal.Interval(point, point),
                    admitted,
                )

        scored.sort(key=lambda item: (item[0], -item[1]))
        pool = scored[: self._TEMPORAL_POOL]
        span = max((window.end - window.start).total_seconds(), 1.0)
        buckets: dict[int, list[tuple[int, float, datetime, RetrievalHit]]] = {}
        for item in pool:
            index = min(
                int(((item[2] - window.start).total_seconds() / span)
                    * self._TEMPORAL_BUCKETS),
                self._TEMPORAL_BUCKETS - 1,
            )
            buckets.setdefault(max(index, 0), []).append(item)
        selected: list[RetrievalHit] = []
        while len(selected) < self._TEMPORAL_KEEP and any(buckets.values()):
            for index in sorted(buckets):
                if buckets[index] and len(selected) < self._TEMPORAL_KEEP:
                    selected.append(buckets[index].pop(0)[3])
        return selected

    @staticmethod
    def _rrf_fuse(
        arms: dict[str, list[RetrievalHit]], k: int
    ) -> list[RetrievalHit]:
        fused: dict[tuple[str, str], RetrievalHit] = {}
        scores: dict[tuple[str, str], float] = {}
        ranks: dict[tuple[str, str], dict[str, int]] = {}
        extra_metadata: dict[tuple[str, str], dict[str, Any]] = {}
        for arm, hits in arms.items():
            for rank, hit in enumerate(hits, 1):
                key = (hit.source_kind, hit.id)
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
                ranks.setdefault(key, {})[arm] = rank
                if key not in fused:
                    fused[key] = hit
                else:
                    # A later arm's copy may carry arm-specific annotations
                    # (e.g. temporal_arm); keep them without overwriting.
                    for meta_key, meta_value in hit.metadata.items():
                        if meta_key not in fused[key].metadata:
                            extra_metadata.setdefault(key, {})[meta_key] = meta_value
        result = []
        for key, hit in fused.items():
            metadata = dict(hit.metadata)
            metadata.update(extra_metadata.get(key, {}))
            metadata["fusion_ranks"] = ranks[key]
            result.append(replace(hit, score=scores[key], metadata=metadata))
        return result

    @staticmethod
    def _apply_boosts(
        hits: list[RetrievalHit], *, now: datetime
    ) -> list[RetrievalHit]:
        """Multiplicative-around-1 secondary signals: nudge, never flip."""
        boosted = []
        for hit in hits:
            # Recency anchors on admission time; valid_from is not in hit
            # metadata at this stage (temporal controls annotate later), so
            # claiming a COALESCE here would be dead code (review L6).
            effective = hit.created_at
            try:
                days = max(
                    0.0, (now - datetime.fromisoformat(effective)).total_seconds() / 86400
                )
                recency = max(0.1, 1.0 - days / 365.0)
            except ValueError:
                recency = 0.5
            temporal_signal = hit.metadata.get("temporal_arm", {}).get("proximity", 0.5)
            support = 0.6 if hit.metadata.get("authority_level") == "confirmed" else 0.5
            factor = (
                (1 + 0.2 * (recency - 0.5))
                * (1 + 0.2 * (temporal_signal - 0.5))
                * (1 + 0.1 * (support - 0.5))
            )
            metadata = dict(hit.metadata)
            metadata["boost_signals"] = {
                "recency": round(recency, 4),
                "temporal": round(temporal_signal, 4),
                "support": support,
            }
            boosted.append(replace(hit, score=hit.score * factor, metadata=metadata))
        return boosted

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
                # Under temporal controls the superseded filter must not run
                # against *current* lineage: the version that was live at the
                # known-at cutoff may be superseded now, and its successor's
                # text may no longer match the query. The temporal pass
                # re-applies as-of supersession itself.
                include_superseded=context.temporal_active,
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

        # Multi-arm fusion (task5.md B2). A second arm exists when the query
        # carries a temporal cue; the legacy single-arm path stays untouched
        # so existing callers keep byte-identical ordering.
        window = (
            temporal.parse_query_window(context.query, now=datetime.now(UTC))
            if context.query else None
        )
        if window is not None and context.query:
            temporal_hits = self._temporal_arm(context, window)
            if temporal_hits:
                # Dedup within the base arm first (review finding M1): an
                # FTS copy and a semantic copy of the same hit must not
                # double-collect reciprocal rank mass.
                base_best: dict[tuple[str, str], RetrievalHit] = {}
                for hit in hits:
                    key = (hit.source_kind, hit.id)
                    if key not in base_best or hit.score > base_best[key].score:
                        base_best[key] = hit
                lexical_sorted = sorted(
                    base_best.values(),
                    key=lambda hit: (hit.score, hit.created_at),
                    reverse=True,
                )
                # v1 arms: "base" is the merged lexical(+semantic) order;
                # splitting semantic into its own arm is a later refinement.
                fused = self._rrf_fuse(
                    {"base": lexical_sorted, "temporal": temporal_hits},
                    self._RRF_K,
                )
                hits = self._apply_boosts(fused, now=datetime.now(UTC))
        deduplicated: dict[tuple[str, str], RetrievalHit] = {}
        for hit in hits:
            key = (hit.source_kind, hit.id)
            if key not in deduplicated or hit.score > deduplicated[key].score:
                deduplicated[key] = hit
        selected = list(deduplicated.values())
        if context.temporal_active:
            selected = self._apply_temporal_controls(selected, context)
        return sorted(
            selected, key=lambda hit: (hit.score, hit.created_at), reverse=True
        )[: context.limit]

    @staticmethod
    def _ancestor_as_of(
        memory_id: str,
        by_id: dict[str, MemoryRecord],
        links: dict[str, str | None],
    ) -> MemoryRecord | None:
        """Walk the supersedes lineage down to the version visible at the
        known-at cutoff. The queried version may postdate the cutoff while an
        ancestor was the live version at that time (task4.md invariant 2).
        ``links`` is the one-query lineage map — no per-hop store reads."""
        seen: set[str] = set()
        current_id: str | None = memory_id
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            record = by_id.get(current_id)
            if record is not None:
                return record
            current_id = links.get(current_id)
        return None

    def _apply_temporal_controls(
        self, hits: list[RetrievalHit], context: RetrievalContext
    ) -> list[RetrievalHit]:
        cutoff = None
        if context.known_at:
            cutoff = self.store.known_at_cutoff_seq(
                context.known_at, branch_id=context.branch_id
            )
        versions = self.store.memories_as_of(
            branch_id=context.branch_id, cutoff_seq=cutoff
        )
        by_id = {record.id: record for record in versions}
        successor_of = {
            record.supersedes_id: record for record in versions if record.supersedes_id
        }
        lineage_links = self.store.memory_lineage_links(branch_id=context.branch_id)
        observed_at_map = self.store.events_created_at(
            [record.source_event_ids[0] for record in versions if record.source_event_ids]
        )
        reference: temporal.Envelope | None = None
        if context.valid_at:
            reference = temporal.normalize_bound(context.valid_at).envelope
        # validity_status is always the trust level of *now* (task4.md
        # invariant 4); a valid_at match is reported separately so an
        # expired fact matched at a past instant is never labeled current.
        evaluation_point = temporal.now_envelope(datetime.now(UTC))

        def annotate(hit: RetrievalHit, extra: dict[str, Any]) -> RetrievalHit:
            metadata = dict(hit.metadata)
            metadata.update(extra)
            metadata["temporal_projection_code_version"] = (
                temporal.TEMPORAL_PROJECTION_CODE_VERSION
            )
            if cutoff is not None:
                metadata["known_at_cutoff_seq"] = cutoff
            return replace(hit, metadata=metadata)

        result: list[RetrievalHit] = []
        emitted: set[str] = set()

        def process(record: MemoryRecord, hit: RetrievalHit) -> None:
            if record.id in emitted:
                return
            successor = successor_of.get(record.id)
            if successor is not None and not context.history:
                return
            interval = temporal.interval_from_fields(
                record.valid_from, record.valid_from_precision,
                record.valid_to, record.valid_to_precision,
            )
            successor_start = (
                temporal.envelope_from_fields(
                    successor.valid_from, successor.valid_from_precision
                )
                if successor is not None
                else None
            )
            effective = temporal.Interval(
                interval.start, temporal.effective_end(interval, successor_start)
            )
            status = temporal.validity_status(effective, evaluation_point)
            match = None
            if context.valid_at:
                match = temporal.contains(effective, reference)
                if match is temporal.Truth.FALSE:
                    return
                if match is not temporal.Truth.TRUE and not context.include_unknown_validity:
                    return
            elif context.current:
                if status in ("expired", "not_yet_valid"):
                    return
                if status == "unknown" and not context.include_unknown_validity:
                    return
            extra: dict[str, Any] = {
                "validity_status": status,
                "temporal": {
                    "valid_from": record.valid_from,
                    "valid_from_precision": record.valid_from_precision,
                    "valid_to": record.valid_to,
                    "valid_to_precision": record.valid_to_precision,
                    "temporal_expression": record.temporal_expression,
                    "observed_at": (
                        observed_at_map.get(record.source_event_ids[0])
                        if record.source_event_ids
                        else None
                    ),
                },
            }
            if match is not None:
                extra["temporal_match"] = (
                    "definite" if match is temporal.Truth.TRUE else "possible"
                )
            if successor is not None:
                extra["superseded_by"] = successor.id
                if successor_start is not None and successor_start.known:
                    extra["effective_valid_to"] = successor.valid_from
            emitted.add(record.id)
            result.append(annotate(hit, extra))

        for hit in hits:
            if hit.source_kind != "memory":
                if cutoff is not None:
                    # Fail closed: a hit that cannot prove admission before the
                    # cutoff (e.g. from a plugin that omits seq) is excluded.
                    try:
                        seq_value = int(hit.metadata["seq"])
                    except (KeyError, TypeError, ValueError):
                        try:
                            seq_value = self.store.get_event(hit.id).seq
                        except KeyError:
                            continue
                    if seq_value > cutoff:
                        continue
                if (context.valid_at or context.current) and not (
                    context.include_unknown_validity
                ):
                    # Events carry transaction time only; they cannot prove
                    # validity and are excluded from validity-filtered results.
                    continue
                result.append(annotate(hit, {"validity_status": "unknown"}))
                continue
            record = by_id.get(hit.id)
            if record is None:
                record = self._ancestor_as_of(hit.id, by_id, lineage_links)
                if record is None:
                    continue
                hit = self._memory_hit(record, context)
            process(record, hit)
            if context.history:
                # Surface the full lineage visible at the cutoff, not only the
                # version the query happened to match.
                ancestor = by_id.get(record.supersedes_id or "")
                while ancestor is not None and ancestor.id not in emitted:
                    process(ancestor, self._memory_hit(ancestor, context))
                    ancestor = by_id.get(ancestor.supersedes_id or "")
        return result

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
