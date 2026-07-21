from __future__ import annotations

from typing import Protocol


PUBLIC_API = "public_api"
HOST_HOOK = "host_hook"
INTERNAL = "internal"
LEGACY_UNTRUSTED = "legacy_untrusted"
ORIGIN_CHANNELS = frozenset({PUBLIC_API, HOST_HOOK, INTERNAL, LEGACY_UNTRUSTED})

# Manual settlement and workstream lifecycle commands ride canonical internal
# request events. Public API/MCP text cannot mint the internal channel; the
# stored payload is process-authored evidence, never a caller claim.
SETTLEMENT_REQUEST_OPERATION = "settlement_requested"
WORKSTREAM_REQUEST_OPERATION = "workstream_transition_requested"


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
        and event.payload.get("_joiny_origin_adapter") == event.origin_adapter
    ):
        return HOST_ASSISTANT_FINALIZATION
    if (
        event.origin_channel == INTERNAL
        and event.payload.get("operation") == "policy_bootstrapped"
    ):
        return BOOTSTRAP_TOFU
    if (
        event.origin_channel == INTERNAL
        and event.payload.get("operation") in {
            SETTLEMENT_REQUEST_OPERATION,
            WORKSTREAM_REQUEST_OPERATION,
        }
    ):
        requested_by = event.payload.get("requested_by")
        if requested_by == "operator":
            return LOCAL_OPERATOR
        if requested_by == "agent":
            return DELEGATED_AGENT
    return EXTERNAL_UNTRUSTED


def is_host_logical_user(event: EventProvenance) -> bool:
    return origin_evidence_type(event) == HOST_LOGICAL_USER
