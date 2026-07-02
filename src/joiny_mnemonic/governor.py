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
    def _threshold(policy: BudgetPolicy, ratio: float) -> int:
        return math.ceil(policy.context_window_tokens * ratio)

    def decide(
        self,
        *,
        branch_id: str = "main",
        session_id: str | None = None,
    ) -> GovernorDecision:
        policy = self.service.store.get_budget_policy(branch_id=branch_id)
        context_tokens, source = self.estimate_context(
            branch_id=branch_id, session_id=session_id
        )
        ratio = context_tokens / policy.context_window_tokens
        actions: list[str] = []
        reasons: list[str] = []
        if ratio >= policy.snapshot_ratio:
            actions.append("snapshot")
            reasons.append(
                f"context {context_tokens} reached snapshot threshold "
                f"{self._threshold(policy, policy.snapshot_ratio)}"
            )
        if ratio >= policy.compact_ratio:
            actions.append("compact")
            reasons.append(
                f"context ratio {ratio:.3f} reached compact ratio {policy.compact_ratio:.3f}"
            )
        if ratio >= policy.handoff_ratio:
            actions.append("handoff")
            reasons.append(
                f"context ratio {ratio:.3f} reached handoff ratio {policy.handoff_ratio:.3f}"
            )
        if ratio >= policy.hard_limit_ratio:
            actions.append("handoff_required")
            reasons.append(
                f"context ratio {ratio:.3f} reached hard limit {policy.hard_limit_ratio:.3f}"
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

    def register_context_warning(
        self,
        counter: HookContextCounter,
        *,
        branch_id: str,
        session_id: str,
        source_event: Event,
    ) -> bool:
        if counter.cumulative_tokens < counter.threshold_tokens:
            return False
        policy = self.service.store.get_budget_policy(branch_id=branch_id)
        receipt_key = f"context-warning:{session_id}:{policy.id}:{counter.threshold_tokens}"
        created = self.service.store.record_governor_action(
            receipt_key=receipt_key,
            branch_id=branch_id,
            session_id=session_id,
            source_event_id=source_event.id,
            action="context_warning",
            reason=(
                f"cumulative raw hook context {counter.cumulative_tokens} reached "
                f"early warning threshold {counter.threshold_tokens}"
            ),
            context_tokens=counter.cumulative_tokens,
            threshold_tokens=counter.threshold_tokens,
            payload={
                "policy_id": policy.id,
                "source": "hook-cumulative-raw-estimate",
                "event_name": counter.event_name,
                "increment_tokens": counter.increment_tokens,
            },
        )
        return created or self.service.store.governor_action_source(receipt_key) == source_event.id

    def evaluate_and_apply(
        self,
        *,
        branch_id: str = "main",
        session_id: str | None = None,
        source_event: Event | None = None,
    ) -> GovernorDecision:
        decision = self.decide(branch_id=branch_id, session_id=session_id)
        if not decision.actions:
            return decision
        policy = self.service.store.get_budget_policy(branch_id=branch_id)
        current_seq = source_event.seq if source_event is not None else (
            self.service.store.query_events(branch_id=branch_id)[-1].seq
            if self.service.store.query_events(branch_id=branch_id) else 0
        )
        applied: list[str] = []
        reasons: list[str] = []
        snapshot_created = False
        for action in decision.actions:
            last_seq = self.service.store.last_governor_action_seq(branch_id, action)
            if last_seq is not None and current_seq - last_seq < policy.min_action_interval_events:
                continue
            ratio = {
                "snapshot": policy.snapshot_ratio,
                "compact": policy.compact_ratio,
                "handoff": policy.handoff_ratio,
                "handoff_required": policy.hard_limit_ratio,
            }[action]
            threshold = self._threshold(policy, ratio)
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
                reason=next(
                    (reason for reason in decision.reasons if action.split("_")[0] in reason),
                    decision.reasons[0] if decision.reasons else action,
                ),
                context_tokens=decision.context_tokens,
                threshold_tokens=threshold,
                payload={"policy_id": policy.id, "source": decision.source},
            )
            if not created:
                continue
            if action == "snapshot" and not snapshot_created:
                self.service.create_snapshot(branch_id=branch_id)
                snapshot_created = True
            elif action == "compact":
                if not snapshot_created:
                    self.service.create_snapshot(branch_id=branch_id)
                    snapshot_created = True
                self.service.compact(branch_id=branch_id)
            applied.append(action)
            reasons.append(
                next(
                    (reason for reason in decision.reasons if action.split("_")[0] in reason),
                    action,
                )
            )
        return GovernorDecision(
            branch_id=decision.branch_id,
            context_tokens=decision.context_tokens,
            context_ratio=decision.context_ratio,
            actions=tuple(applied),
            reasons=tuple(reasons),
            policy_id=decision.policy_id,
            source=decision.source,
        )