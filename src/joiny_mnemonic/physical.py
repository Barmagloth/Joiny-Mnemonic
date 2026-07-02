from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Placement(StrEnum):
    TEXT_RECOMPUTE = "text_recompute"
    GPU_KV = "gpu_kv"
    CPU_KV = "cpu_kv"
    CPU_QUANTIZED_KV = "cpu_quantized_kv"
    OFFLOAD = "offload"


@dataclass(frozen=True, slots=True)
class PhysicalCandidate:
    placement: Placement
    bytes_required: int
    restore_latency_ms: float
    recompute_latency_ms: float
    expected_uses: float
    available: bool = True


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    placement: Placement
    estimated_total_latency_ms: float
    bytes_required: int
    rationale: str


class PhysicalMemoryGovernor:
    """Choose storage versus recompute without prescribing KV compression."""

    def choose(
        self,
        candidates: list[PhysicalCandidate],
        *,
        memory_budget_bytes: int,
        latency_weight: float = 1.0,
        byte_weight: float = 1e-7,
    ) -> PlacementDecision:
        eligible = [
            item for item in candidates
            if item.available and item.bytes_required <= memory_budget_bytes
        ]
        if not eligible:
            raise ValueError("no physical-memory candidate fits the supplied budget")

        def total_cost(item: PhysicalCandidate) -> float:
            latency = (
                item.recompute_latency_ms if item.placement == Placement.TEXT_RECOMPUTE
                else item.restore_latency_ms
            ) * item.expected_uses
            return latency_weight * latency + byte_weight * item.bytes_required

        winner = min(eligible, key=total_cost)
        latency = (
            winner.recompute_latency_ms if winner.placement == Placement.TEXT_RECOMPUTE
            else winner.restore_latency_ms
        ) * winner.expected_uses
        return PlacementDecision(
            placement=winner.placement,
            estimated_total_latency_ms=latency,
            bytes_required=winner.bytes_required,
            rationale=(
                "minimum context-specific latency/storage cost among available tiers; "
                "attention scores are not durability or truth signals"
            ),
        )
