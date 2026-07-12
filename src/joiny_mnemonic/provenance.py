from __future__ import annotations

from typing import Protocol


PUBLIC_API = "public_api"
HOST_HOOK = "host_hook"
INTERNAL = "internal"
LEGACY_UNTRUSTED = "legacy_untrusted"


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
    return "external_untrusted"


def is_host_logical_user(event: EventProvenance) -> bool:
    return origin_evidence_type(event) == "host_logical_user"
