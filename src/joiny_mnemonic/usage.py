from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .models import Event, PromptPacket, UsageSample
from .prompt import conservative_token_estimate
from .reducers import ReductionBundle
from .storage import MemoryStore


@dataclass(frozen=True, slots=True)
class HookContextCounter:
    event_name: str
    increment_tokens: int
    cumulative_tokens: int
    threshold_tokens: int
    context_window_tokens: int
    ratio: float
    crossed_threshold: bool
    estimated: bool = True


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        return (
            input_tokens * self.input_per_million
            + output_tokens * self.output_per_million
            + cache_read_tokens * self.cache_read_per_million
            + cache_write_tokens * self.cache_write_per_million
        ) / 1_000_000


_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "completion_tokens", "outputTokens"),
    "cache_read_tokens": ("cache_read_tokens", "cache_read_input_tokens", "cached_tokens", "cacheReadTokens"),
    "cache_write_tokens": ("cache_write_tokens", "cache_creation_input_tokens", "cacheWriteTokens"),
    "context_tokens": ("context_tokens", "context_window_used", "contextTokens", "total_context_tokens"),
    "cost_usd": ("cost_usd", "costUSD", "total_cost", "cost"),
    "latency_ms": ("latency_ms", "duration_ms", "latencyMs"),
}


def _usage_mapping(value: dict[str, Any]) -> dict[str, Any]:
    candidates = [value]
    for key in ("usage", "token_usage", "response", "metadata"):
        nested = value.get(key)
        if isinstance(nested, dict):
            candidates.insert(0, nested)
            nested_usage = nested.get("usage")
            if isinstance(nested_usage, dict):
                candidates.insert(0, nested_usage)
    result: dict[str, Any] = {}
    for target, aliases in _ALIASES.items():
        for candidate in candidates:
            match = next((candidate[name] for name in aliases if candidate.get(name) is not None), None)
            if match is not None:
                result[target] = match
                break
    return result


class UsageMeter:
    """Persist provider-reported usage and explicitly labelled local estimates."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def capture_native(
        self,
        payload: dict[str, Any],
        *,
        source: str,
        branch_id: str,
        session_id: str | None,
        event_id: str | None = None,
        receipt_key: str | None = None,
    ) -> UsageSample | None:
        usage = _usage_mapping(payload)
        if not usage:
            return None
        return self.store.record_usage(
            branch_id=branch_id,
            session_id=session_id,
            event_id=event_id,
            source=source,
            operation="model_usage",
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_tokens", 0)),
            cache_write_tokens=int(usage.get("cache_write_tokens", 0)),
            context_tokens=int(usage.get("context_tokens", usage.get("input_tokens", 0))),
            estimated=False,
            cost_usd=float(usage["cost_usd"]) if usage.get("cost_usd") is not None else None,
            latency_ms=float(usage["latency_ms"]) if usage.get("latency_ms") is not None else None,
            metadata={"provider_reported": True},
            receipt_key=receipt_key,
        )

    def record_hook_context(
        self,
        events: tuple[Event, ...] | list[Event],
        *,
        event_name: str,
        branch_id: str,
        session_id: str,
        receipt_key: str,
        context_window_tokens: int,
        threshold_tokens: int,
    ) -> HookContextCounter | None:
        if event_name not in {"UserPromptSubmit", "PostToolUse"}:
            return None
        parts: list[str] = []
        for event in events:
            if event.kind == "tool_call":
                parts.append(event.content)
                tool_input = event.payload.get("tool_input")
                if tool_input is not None:
                    parts.append(json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str))
            else:
                parts.append(event.content)
        # Include conservative role/tool framing that the native transcript also carries.
        increment = conservative_token_estimate("\n".join(parts)) + 16 * len(events)
        stored_increment, cumulative, _created = self.store.record_hook_context_increment(
            receipt_key=f"hook-context:{receipt_key}",
            branch_id=branch_id,
            session_id=session_id,
            event_id=events[-1].id if events else None,
            event_name=event_name,
            increment_tokens=increment,
        )
        previous = cumulative - stored_increment
        return HookContextCounter(
            event_name=event_name,
            increment_tokens=stored_increment,
            cumulative_tokens=cumulative,
            threshold_tokens=threshold_tokens,
            context_window_tokens=context_window_tokens,
            ratio=cumulative / context_window_tokens,
            crossed_threshold=previous < threshold_tokens <= cumulative,
        )

    def record_reduction(
        self,
        event: Event,
        bundle: ReductionBundle,
        *,
        emitted_tokens: int,
        emitted_bytes: int,
    ) -> UsageSample:
        return self.store.record_usage(
            branch_id=event.branch_id,
            session_id=event.session_id,
            event_id=event.id,
            source="joiny-mnemonic",
            operation="tool_output_reduce",
            input_tokens=bundle.raw_tokens,
            output_tokens=emitted_tokens,
            context_tokens=0,
            estimated=True,
            latency_ms=bundle.latency_ns / 1_000_000,
            raw_bytes=bundle.raw_bytes,
            emitted_bytes=emitted_bytes,
            metadata={
                "family": bundle.family,
                "critical_signal_count": bundle.critical_signal_count,
                "critical_signal_recall": bundle.compact_critical_recall,
                "token_savings": max(0, bundle.raw_tokens - emitted_tokens),
            },
            receipt_key=f"reduce:{event.id}:v1",
        )

    def record_source_promotion(
        self,
        *,
        branch_id: str,
        target_id: str,
        family: str,
        session_id: str | None = None,
    ) -> UsageSample:
        """task5.md D5: a promotion to exact source shortly after a compact
        exposure is the ground-truth signal of over-compression (Headroom's
        TOIN insight, Apache-2.0, de-LLM-ified into plain correlation)."""
        return self.store.record_usage(
            branch_id=branch_id,
            session_id=session_id,
            source="joiny-mnemonic",
            operation="source_promotion",
            estimated=False,
            metadata={"target": target_id, "family": family},
        )

    def overcompression_report(self, *, branch_id: str = "main") -> dict[str, Any]:
        """Deterministic offline correlation: reduce exposures vs promotions
        per reducer family. Warning-only; feeding it back into reducer levels
        is a human decision."""
        reduces: dict[str, int] = {}
        promotions: dict[str, int] = {}
        for sample in self.store.list_usage_samples(branch_id=branch_id):
            family = str(sample.metadata.get("family", "")) or "unknown"
            if sample.operation == "tool_output_reduce":
                reduces[family] = reduces.get(family, 0) + 1
            elif sample.operation == "source_promotion":
                promotions[family] = promotions.get(family, 0) + 1
        families = {}
        for family in sorted(set(reduces) | set(promotions)):
            reduce_count = reduces.get(family, 0)
            promotion_count = promotions.get(family, 0)
            ratio = promotion_count / reduce_count if reduce_count else 0.0
            families[family] = {
                "reduced_views": reduce_count,
                "source_promotions": promotion_count,
                "promotion_ratio": round(ratio, 4),
                "over_compressed": reduce_count >= 5 and ratio > 0.2,
            }
        return {
            "branch_id": branch_id,
            "families": families,
            "recommendation": [
                family for family, item in families.items() if item["over_compressed"]
            ],
        }

    def record_retrieval_search(
        self,
        *,
        branch_id: str,
        query: str,
        hits: list[Any],
        semantic_enabled: bool,
        filters: dict[str, Any],
        limit: int,
        session_id: str | None = None,
        task_key: str | None = None,
        receipt_key: str | None = None,
    ) -> UsageSample:
        return self.store.record_usage(
            branch_id=branch_id,
            session_id=session_id,
            source="joiny-mnemonic",
            operation="retrieval_search",
            input_tokens=conservative_token_estimate(query),
            estimated=True,
            raw_bytes=len(query.encode("utf-8")),
            metadata={
                "query": query,
                "task_key": task_key,
                "semantic_enabled": semantic_enabled,
                "filters": filters,
                "limit": limit,
                "results": [
                    {
                        "id": hit.id,
                        "score": hit.score,
                        "source_kind": hit.source_kind,
                        "position": position,
                        "origin": hit.metadata.get("origin"),
                        "candidate_id": hit.metadata.get("candidate_id"),
                        "extraction_run_id": hit.metadata.get("extraction_run_id"),
                        "extractor_config_hash": hit.metadata.get("extractor_config_hash"),
                    }
                    for position, hit in enumerate(hits)
                ],
            },
            receipt_key=receipt_key,
        )

    def record_prompt_injection(
        self,
        packet: PromptPacket,
        *,
        branch_id: str,
        query: str,
        session_id: str | None = None,
        task_key: str | None = None,
        latency_ms: float | None = None,
        receipt_key: str | None = None,
    ) -> UsageSample:
        return self.store.record_usage(
            branch_id=branch_id,
            session_id=session_id,
            source="joiny-mnemonic",
            operation="prompt_injection",
            input_tokens=conservative_token_estimate(query),
            output_tokens=packet.estimated_tokens,
            context_tokens=packet.estimated_tokens,
            estimated=True,
            latency_ms=latency_ms,
            raw_bytes=len(query.encode("utf-8")),
            emitted_bytes=len(packet.text.encode("utf-8")),
            metadata={
                "query": query,
                "task_key": task_key,
                "included_event_ids": packet.included_event_ids,
                "included_memory_ids": packet.included_memory_ids,
                "memory_lineage": [
                    {
                        "memory_id": memory_id,
                        "origin": record.metadata.get("origin"),
                        "candidate_id": record.metadata.get("candidate_id"),
                        "extraction_run_id": record.metadata.get("extraction_run_id"),
                        "extractor_config_hash": record.metadata.get(
                            "extractor_config_hash"
                        ),
                    }
                    for memory_id in packet.included_memory_ids
                    for record in (self.store.get_memory(memory_id),)
                ],
                "snapshot_id": packet.snapshot_id,
                "token_budget": packet.token_budget,
                "estimated_emitted_tokens": packet.estimated_tokens,
                "stale_reasons": packet.stale_reasons,
            },
            receipt_key=receipt_key,
        )

    def record_prompt(
        self,
        packet: PromptPacket,
        *,
        branch_id: str,
        session_id: str | None = None,
        operation: str = "resume_packet",
        latency_ms: float | None = None,
    ) -> UsageSample:
        return self.store.record_usage(
            branch_id=branch_id,
            session_id=session_id,
            source="joiny-mnemonic",
            operation=operation,
            output_tokens=packet.estimated_tokens,
            context_tokens=packet.estimated_tokens,
            estimated=True,
            latency_ms=latency_ms,
            emitted_bytes=len(packet.text.encode("utf-8")),
            metadata={
                "token_budget": packet.token_budget,
                "snapshot_id": packet.snapshot_id,
                "included_event_count": len(packet.included_event_ids),
                "included_memory_count": len(packet.included_memory_ids),
            },
        )

    def report(self, *, branch_id: str = "main", session_id: str | None = None) -> dict[str, Any]:
        return self.store.usage_report(branch_id=branch_id, session_id=session_id)