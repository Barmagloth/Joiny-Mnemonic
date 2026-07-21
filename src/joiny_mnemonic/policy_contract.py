from __future__ import annotations


PUBLIC_POLICY_FLAGS = frozenset({
    "automatic_extraction_enabled",
    "automatic_task_closure_enabled",
    "agent_settlement_delegation_enabled",
})


def policy_activation_allowed(origin: str) -> bool:
    """Policy-ledger trust semantics, kept out of storage primitives."""
    from .provenance import BOOTSTRAP_TOFU, HOST_LOGICAL_USER

    return origin == BOOTSTRAP_TOFU or origin == HOST_LOGICAL_USER
