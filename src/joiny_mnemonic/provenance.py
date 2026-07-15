from __future__ import annotations

from typing import Protocol


PUBLIC_API = "public_api"
HOST_HOOK = "host_hook"
INTERNAL = "internal"
LEGACY_UNTRUSTED = "legacy_untrusted"

# task6.md 6C: manual settlement rides one canonical request event. The
# internal channel cannot be minted by public-API or MCP text (surfaces
# hardcode origin_channel), so the payload below is process-authored fact,
# not a caller claim.
SETTLEMENT_REQUEST_OPERATION = "settlement_requested"


class EventProvenance(Protocol):
    role: str | None
    origin_channel: str
    payload: dict[str, object]


def origin_evidence_type(event: EventProvenance) -> str:
    """Derive authority from immutable canonical provenance, never caller claims."""
    if event.origin_channel == HOST_HOOK and (event.role or "").casefold() == "user":
        return "host_logical_user"
    if (
        event.origin_channel == INTERNAL
        and event.payload.get("operation") == "policy_bootstrapped"
    ):
        return "bootstrap_tofu"
    if (
        event.origin_channel == INTERNAL
        and event.payload.get("operation") == SETTLEMENT_REQUEST_OPERATION
    ):
        requested_by = event.payload.get("requested_by")
        if requested_by == "operator":
            return "local_operator"
        if requested_by == "agent":
            return "delegated_agent"
    return "external_untrusted"


def is_host_logical_user(event: EventProvenance) -> bool:
    return origin_evidence_type(event) == "host_logical_user"
