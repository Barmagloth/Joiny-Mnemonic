from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, Mapping

from .provenance import (
    BOOTSTRAP_TOFU,
    DELEGATED_AGENT,
    HOST_LOGICAL_USER,
    LOCAL_OPERATOR,
)


WORKSTREAM_FLOW = {
    "active": frozenset({"blocked", "completed", "cancelled"}),
    "blocked": frozenset({"active", "completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}
CANDIDATE_FLOW = {
    "auto": frozenset({
        "confirmation_requested", "rejection_requested", "supersession_requested"
    }),
    "quarantined": frozenset({"confirmation_requested", "rejection_requested"}),
    "confirmation_requested": frozenset({"confirmed", "rejected"}),
    "rejection_requested": frozenset({"rejected"}),
    "supersession_requested": frozenset({"superseded"}),
    "confirmed": frozenset({"supersession_requested"}),
    "rejected": frozenset(),
    "superseded": frozenset(),
}
FINDING_FLOW = {
    "active": frozenset({"acknowledgement_requested"}),
    "acknowledgement_requested": frozenset({"acknowledged"}),
    "acknowledged": frozenset(),
}
SETTLEMENT_FLOW = {
    "pending": frozenset({"applied", "contested"}),
    "applied": frozenset({"reverted", "contested"}),
    "reverted": frozenset(),
    "contested": frozenset(),
}

USER_OR_OPERATOR = frozenset({HOST_LOGICAL_USER, LOCAL_OPERATOR})
USER_OPERATOR_OR_DELEGATE = frozenset({
    HOST_LOGICAL_USER, LOCAL_OPERATOR, DELEGATED_AGENT
})


@dataclass(frozen=True, slots=True)
class TransitionRule:
    name: str
    flow: Mapping[str, AbstractSet[str]]
    trusted_targets: AbstractSet[str] = frozenset()
    trusted_origins: AbstractSet[str] = frozenset()
    reopen_origins: AbstractSet[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    changed: bool
    origin: str


def validate_transition(
    rule: TransitionRule,
    *,
    current: str,
    target: str,
    origin: str,
    source_visible: bool,
    delegated_enabled: bool = False,
    reopen: bool = False,
    reason: str = "",
    system_actor: bool = False,
) -> TransitionDecision:
    """Apply cross-entity invariants while entity flow tables stay separate."""
    if not source_visible:
        raise PermissionError("source event is not visible in the target branch")
    if current == target:
        return TransitionDecision(False, origin)
    if origin == DELEGATED_AGENT and not delegated_enabled:
        raise PermissionError(
            "delegated transition requires agent_settlement_delegation_enabled=true"
        )
    if reopen:
        if target != "active" or current not in {"completed", "cancelled"}:
            raise ValueError(f"{rule.name} reopen is only valid from a terminal state")
        if not reason.strip():
            raise ValueError("reopen requires a non-empty reason")
        if origin not in rule.reopen_origins:
            raise PermissionError(f"{rule.name} reopen requires a trusted origin")
        return TransitionDecision(True, origin)
    if target not in rule.flow.get(current, frozenset()):
        raise ValueError(f"illegal {rule.name} transition: {current} -> {target}")
    if (
        not system_actor
        and target in rule.trusted_targets
        and origin not in rule.trusted_origins
    ):
        raise PermissionError(f"{rule.name} transition to {target} requires a trusted origin")
    return TransitionDecision(True, origin)


WORKSTREAM_RULE = TransitionRule(
    "workstream",
    WORKSTREAM_FLOW,
    trusted_targets=frozenset({"completed", "cancelled"}),
    trusted_origins=USER_OPERATOR_OR_DELEGATE,
    reopen_origins=USER_OPERATOR_OR_DELEGATE,
)
CANDIDATE_RULE = TransitionRule(
    "memory candidate",
    CANDIDATE_FLOW,
    trusted_targets=frozenset({"confirmed", "rejected", "superseded"}),
    trusted_origins=USER_OPERATOR_OR_DELEGATE,
)
FINDING_RULE = TransitionRule(
    "security finding",
    FINDING_FLOW,
    trusted_targets=frozenset({"acknowledged"}),
    trusted_origins=USER_OR_OPERATOR,
)
SETTLEMENT_RULE = TransitionRule(
    "settlement",
    SETTLEMENT_FLOW,
    trusted_targets=frozenset({"applied", "reverted", "contested"}),
    trusted_origins=frozenset({
        BOOTSTRAP_TOFU, HOST_LOGICAL_USER, LOCAL_OPERATOR, DELEGATED_AGENT
    }),
)
