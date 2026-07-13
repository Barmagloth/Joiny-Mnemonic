from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .models import BudgetPolicy, Event, GovernorDecision
from .prompt import conservative_token_estimate

if TYPE_CHECKING:
    from .service import MemoryService
    from .usage import HookContextCounter


class BudgetGovernor:
    """Evidence-driven context governor with auditable, rate-limited actions."""

    def __init__(self, service: MemoryService) -> None:
        self.service = service

    def estimate_context(
        self, *, branch_id: str, session_id: str | None = None
    ) -> tuple[int, str]:
        reported = self.service.store.latest_provider_context_usage(
            branch_id=branch_id, session_id=session_id
        )
        hook_total = (
            self.service.store.hook_context_total(
                branch_id=branch_id, session_id=session_id
            )
            if session_id is not None else 0
        )
        if reported is not None or hook_total:
            reported_tokens = reported.context_tokens if reported is not None else 0
            if reported_tokens >= hook_total:
                return reported_tokens, "provider-reported"
            return hook_total, "hook-cumulative-raw-estimate"
        total = sum(
            conservative_token_estimate(event.content)
            for event in self.service.store.query_events(branch_id=branch_id)
        )
        return total, "local-canonical-raw-estimate"

    @staticmethod
    def thresholds(policy: BudgetPolicy) -> dict[str, int]:
        window = policy.context_window_tokens
        physical_handoff = math.ceil(window * policy.handoff_ratio)
        if policy.reserve_tokens:
            physical_handoff = min(physical_handoff, window - policy.reserve_tokens)
        handoff = physical_handoff
        if policy.recommended_handoff_tokens is not None:
            handoff = min(handoff, policy.recommended_handoff_tokens)
        scale = handoff / physical_handoff if physical_handoff else 1.0
        snapshot = max(1, math.ceil(window * policy.snapshot_ratio * scale))
        compact = max(snapshot + 1, math.ceil(window * policy.compact_ratio * scale))
        handoff = max(compact + 1, handoff)
        hard = math.ceil(window * policy.hard_limit_ratio)
        if policy.reserve_tokens:
            hard = min(hard, window - policy.reserve_tokens)
        if hard <= handoff:
            raise ValueError(
                f"invalid policy {policy.id}: hard threshold {hard} must exceed handoff {handoff}"
            )
        return {
            "snapshot": snapshot,
            "compact": compact,
            "handoff": handoff,
            "handoff_required": hard,
        }

    def decide(
        self,
        *,
        branch_id: str = "main",
        session_id: str | None = None,
        agent: str | None = None,
    ) -> GovernorDecision:
        policy = self.service.budget_policy(branch_id=branch_id, agent=agent)
        thresholds = self.thresholds(policy)
        context_tokens, source = self.estimate_context(
            branch_id=branch_id, session_id=session_id
        )
        ratio = context_tokens / policy.context_window_tokens
        actions: list[str] = []
        reasons: list[str] = []
        labels = {
            "snapshot": "snapshot",
            "compact": "compaction",
            "handoff": "recommended handoff",
            "handoff_required": "hard handoff",
        }
        for action in ("snapshot", "compact", "handoff", "handoff_required"):
            threshold = thresholds[action]
            if context_tokens >= threshold:
                actions.append(action)
                reasons.append(
                    f"context {context_tokens} reached {labels[action]} threshold {threshold}"
                )
        return GovernorDecision(
            branch_id=branch_id,
            context_tokens=context_tokens,
            context_ratio=ratio,
            actions=tuple(actions),
            reasons=tuple(reasons),
            policy_id=policy.id,
            source=source,
        )

    def register_context_checkpoint(
        self,
        counter: HookContextCounter,
        *,
        branch_id: str,
        session_id: str,
        source_event: Event,
        agent: str | None = None,
    ) -> bool:
        if counter.cumulative_tokens < counter.threshold_tokens:
            return False
        policy = self.service.budget_policy(branch_id=branch_id, agent=agent)
        receipt_key = f"context-checkpoint:{session_id}:{policy.id}:{counter.threshold_tokens}"
        if self.service.store.governor_action_source(receipt_key) == source_event.id:
            return True
        tail_bytes = self.service.store.snapshot_replay_tail_size(branch_id=branch_id)
        tail_threshold_bytes = max(1, counter.threshold_tokens * 4)
        if tail_bytes < tail_threshold_bytes:
            return False
        created = self.service.store.record_governor_action(
            receipt_key=receipt_key,
            branch_id=branch_id,
            session_id=session_id,
            source_event_id=source_event.id,
            action="context_checkpoint",
            reason=(
                f"cumulative raw hook context {counter.cumulative_tokens} reached "
                f"snapshot threshold {counter.threshold_tokens}"
            ),
            context_tokens=counter.cumulative_tokens,
            threshold_tokens=counter.threshold_tokens,
            payload={
                "policy_id": policy.id,
                "agent": agent,
                "source": "hook-cumulative-raw-estimate",
                "event_name": counter.event_name,
                "increment_tokens": counter.increment_tokens,
            },
        )
        return created

    def evaluate_and_apply(
        self,
        *,
        branch_id: str = "main",
        session_id: str | None = None,
        source_event: Event | None = None,
        agent: str | None = None,
    ) -> GovernorDecision:
        decision = self.decide(branch_id=branch_id, session_id=session_id, agent=agent)
        if not decision.actions:
            return decision
        policy = self.service.budget_policy(branch_id=branch_id, agent=agent)
        thresholds = self.thresholds(policy)
        current_seq = source_event.seq if source_event is not None else (
            self.service.store.query_events(branch_id=branch_id)[-1].seq
            if self.service.store.query_events(branch_id=branch_id) else 0
        )
        applied: list[str] = []
        reasons: list[str] = []
        reason_by_action = dict(zip(decision.actions, decision.reasons, strict=True))
        snapshot_created = False
        snapshot_tail_bytes = self.service.store.snapshot_replay_tail_size(branch_id=branch_id)
        snapshot_tail_threshold = max(1, thresholds["snapshot"] * 4)
        snapshot_due = snapshot_tail_bytes >= snapshot_tail_threshold
        for action in decision.actions:
            if action == "snapshot" and not snapshot_due:
                continue
            last_seq = self.service.store.last_governor_action_seq(
                branch_id, action, policy_id=policy.id
            )
            if last_seq is not None and current_seq - last_seq < policy.min_action_interval_events:
                continue
            threshold = thresholds[action]
            bucket_width = max(1, math.ceil(policy.context_window_tokens * 0.05))
            receipt = (
                f"governor:{branch_id}:{policy.id}:{action}:"
                f"{decision.context_tokens // bucket_width}"
            )
            created = self.service.store.record_governor_action(
                receipt_key=receipt,
                branch_id=branch_id,
                session_id=session_id,
                source_event_id=source_event.id if source_event is not None else None,
                action=action,
                reason=reason_by_action[action],

                context_tokens=decision.context_tokens,
                threshold_tokens=threshold,
                payload={
                    "policy_id": policy.id,
                    "agent": agent,
                    "source": decision.source,
                    "snapshot_tail_bytes": snapshot_tail_bytes,
                    "snapshot_tail_threshold_bytes": snapshot_tail_threshold,
                },
            )
            if not created:
                continue
            if action == "snapshot" and not snapshot_created:
                self.service.create_snapshot(branch_id=branch_id)
                snapshot_created = True
            elif action == "compact":
                if (
                    not snapshot_created
                    and (snapshot_due or self.service.store.latest_snapshot(branch_id=branch_id) is None)
                ):
                    self.service.create_snapshot(branch_id=branch_id)
                    snapshot_created = True
                self.service.compact(branch_id=branch_id)
            applied.append(action)
            reasons.append(reason_by_action[action])

        return GovernorDecision(
            branch_id=decision.branch_id,
            context_tokens=decision.context_tokens,
            context_ratio=decision.context_ratio,
            actions=tuple(applied),
            reasons=tuple(reasons),
            policy_id=decision.policy_id,
            source=decision.source,
        )
