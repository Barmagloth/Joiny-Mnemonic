from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from .finalization_observer import classify_finalization_text
from .models import Event
from .provenance import HOST_ASSISTANT_FINALIZATION, origin_evidence_type

if TYPE_CHECKING:
    from .service import MemoryService


_MEMORY_TYPE = {
    "GOAL": "fact",
    "DECISION": "decision",
    "FACT": "fact",
    "CONSTRAINT": "fact",
    "TODO": "task",
    "PREFERENCE": "preference",
    "FAILURE": "failure",
    "LESSON": "lesson",
}
_AUDIT_LINK_ONLY = re.compile(
    r"^(?:proposal|предложение|task|задача|todo|решение)\s*#?\s*[\w.-]+[.!]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    event_id: str
    record_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    quarantine_ids: tuple[str, ...] = ()
    eligible: bool = False


def _render_audit_content(tag: dict[str, str]) -> str:
    if tag["status"] == "CONFIRMED":
        return tag["text"]
    return f"[{tag['status']}] {tag['text']}"


def materialize_finalizations(
    service: MemoryService, events: Sequence[Event]
) -> tuple[FinalizationResult, ...]:
    """Materialize strict tags only from provenance-proven assistant Stop events."""
    results: list[FinalizationResult] = []
    with service.store._transaction():
        for event in events:
            if origin_evidence_type(event) != HOST_ASSISTANT_FINALIZATION:
                results.append(FinalizationResult(event_id=event.id))
                continue

            classified = classify_finalization_text(event.content)
            quarantine_ids = [
                service.store.quarantine_finalization(
                    source_event_id=event.id,
                    reason_code="malformed_finalization",
                    raw_content=line,
                )
                for line in classified["malformed"]
            ]
            unique: dict[tuple[str, str, str], dict[str, str]] = {}
            statuses: dict[tuple[str, str], set[str]] = {}
            for raw in classified["valid"]:
                tag = {key: str(raw[key]) for key in ("type", "status", "text")}
                if _AUDIT_LINK_ONLY.fullmatch(tag["text"]):
                    quarantine_ids.append(service.store.quarantine_finalization(
                        source_event_id=event.id,
                        reason_code="non_standalone_text",
                        raw_content=(
                            f"[{tag['type']}] {tag['status']}: {tag['text']}"
                        ),
                    ))
                    continue
                unique.setdefault((tag["type"], tag["status"], tag["text"]), tag)
                statuses.setdefault((tag["type"], tag["text"]), set()).add(tag["status"])

            if any(len(values) > 1 for values in statuses.values()):
                for tag in unique.values():
                    quarantine_ids.append(
                        service.store.quarantine_finalization(
                            source_event_id=event.id,
                            reason_code="contradictory_statuses",
                            raw_content=(
                                f"[{tag['type']}] {tag['status']}: {tag['text']}"
                            ),
                        )
                    )
                results.append(FinalizationResult(
                    event_id=event.id,
                    quarantine_ids=tuple(quarantine_ids),
                    eligible=True,
                ))
                continue

            existing = {
                (
                    str(row["finalization_type"]), str(row["status"]),
                    str(row["content"]),
                ): row
                for row in service.store.list_finalizations(branch_id=event.branch_id)
                if str(row["source_event_id"]) == event.id
            }
            record_ids: list[str] = []
            memory_ids: list[str] = []
            for tag in unique.values():
                audit_content = _render_audit_content(tag)
                prior = existing.get((tag["type"], tag["status"], audit_content))
                if prior is not None:
                    record_ids.append(str(prior["id"]))
                    if prior["memory_id"] is not None:
                        memory_ids.append(str(prior["memory_id"]))
                    continue

                memory_id = None
                if tag["status"] == "CONFIRMED":
                    memory = service.derive_memory(
                        memory_type=_MEMORY_TYPE[tag["type"]],
                        content=tag["text"],
                        summary=f"{tag['type'].title()}: {tag['text']}",
                        source_event_ids=(event.id,),
                        branch_id=event.branch_id,
                        metadata={
                            "origin": "host_finalization",
                            "authority_level": "agent_finalized",
                            "origin_evidence_type": HOST_ASSISTANT_FINALIZATION,
                            "finalization_type": tag["type"],
                            "finalization_status": tag["status"],
                        },
                    )
                    memory_id = memory.id
                    memory_ids.append(memory.id)
                record_ids.append(service.store.record_finalization(
                    source_event_id=event.id,
                    branch_id=event.branch_id,
                    finalization_type=tag["type"],
                    status=tag["status"],
                    content=audit_content,
                    memory_id=memory_id,
                ))

            results.append(FinalizationResult(
                event_id=event.id,
                record_ids=tuple(record_ids),
                memory_ids=tuple(memory_ids),
                quarantine_ids=tuple(quarantine_ids),
                eligible=True,
            ))
    return tuple(results)
