from __future__ import annotations

from typing import Protocol


PUBLIC_API = "public_api"
HOST_HOOK = "host_hook"
INTERNAL = "internal"
LEGACY_UNTRUSTED = "legacy_untrusted"
ORIGIN_CHANNELS = frozenset({PUBLIC_API, HOST_HOOK, INTERNAL, LEGACY_UNTRUSTED})

# task6.md 6C: manual settlement rides one canonical request event. The
# internal channel cannot be minted by public-API or MCP text (surfaces
# hardcode origin_channel), so the payload below is process-authored fact,
# not a caller claim.
SETTLEMENT_REQUEST_OPERATION = "settlement_requested"


class EventProvenance(Protocol):
    role: str | None
    origin_channel: str
    origin_adapter: str | None
    payload: dict[str, object]


EXTERNAL_UNTRUSTED = "external_untrusted"
HOST_LOGICAL_USER = "host_logical_user"
HOST_ASSISTANT_FINALIZATION = "host_assistant_finalization"
LOCAL_OPERATOR = "local_operator"
DELEGATED_AGENT = "delegated_agent"
BOOTSTRAP_TOFU = "bootstrap_tofu"
ORIGIN_EVIDENCE_TYPES = frozenset({
    EXTERNAL_UNTRUSTED,
    HOST_LOGICAL_USER,
    HOST_ASSISTANT_FINALIZATION,
    LOCAL_OPERATOR,
    DELEGATED_AGENT,
    BOOTSTRAP_TOFU,
})


def origin_evidence_type(event: EventProvenance) -> str:
    """Derive authority from immutable canonical provenance, never caller claims."""
    if event.origin_channel == HOST_HOOK and (event.role or "").casefold() == "user":
        return HOST_LOGICAL_USER
    if (
        event.origin_channel == HOST_HOOK
        and (event.role or "").casefold() == "assistant"
        and event.payload.get("hook_event_name") == "Stop"
        and bool(event.origin_adapter)
    ):
        return HOST_ASSISTANT_FINALIZATION
    if (
        event.origin_channel == INTERNAL
        and event.payload.get("operation") == "policy_bootstrapped"
    ):
        return BOOTSTRAP_TOFU
    if (
        event.origin_channel == INTERNAL
        and event.payload.get("operation") == SETTLEMENT_REQUEST_OPERATION
    ):
        requested_by = event.payload.get("requested_by")
        if requested_by == "operator":
            return LOCAL_OPERATOR
        if requested_by == "agent":
            return DELEGATED_AGENT
    return EXTERNAL_UNTRUSTED


def is_host_logical_user(event: EventProvenance) -> bool:
    return origin_evidence_type(event) == HOST_LOGICAL_USER
