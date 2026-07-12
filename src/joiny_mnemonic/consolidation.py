from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Sequence

from .models import Event, MemoryRecord
from .prompt import conservative_token_estimate
from .transcript import interaction_groups

if TYPE_CHECKING:
    from .service import MemoryService


_MARKER = re.compile(
    r"^\s*(goal|constraint|decision|task|todo|fact|preference|failed|failure|lesson)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_MEMORY_TYPES = {
    "fact", "decision", "task", "preference", "failure", "lesson", "summary", "index"
}
_BLOCKS = {"instructions", "goal", "constraints", "decisions", "open_tasks"}


@dataclass(frozen=True, slots=True)
class ConsolidationPolicy:
    allow_records: bool
    allow_blocks: bool


def consolidation_policy(event: Event) -> ConsolidationPolicy:
    if event.kind != "message":
        return ConsolidationPolicy(allow_records=False, allow_blocks=False)
    role = (event.role or "").casefold()
    if role == "user":
        return ConsolidationPolicy(allow_records=True, allow_blocks=True)
    if role == "assistant":
        return ConsolidationPolicy(allow_records=True, allow_blocks=False)
    return ConsolidationPolicy(allow_records=False, allow_blocks=False)


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    memory_type: str
    content: str
    summary: str = ""
    block: str | None = None
    risk: float = 0.0
    retrieval_cost: float = 1.0
    files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    event_id: str
    memory_ids: tuple[str, ...]
    block_ids: tuple[str, ...]
    skipped_blocks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompactionResult:
    summary: MemoryRecord | None
    index: MemoryRecord | None
    source_event_ids: tuple[str, ...]
    text: str


class EvidenceConsolidator:
    """Deterministic consolidation that never invents facts beyond source events."""

    MARKER_MAP = {
        "goal": ("fact", "goal"),
        "constraint": ("fact", "constraints"),
        "decision": ("decision", "decisions"),
        "task": ("task", "open_tasks"),
        "todo": ("task", "open_tasks"),
        "fact": ("fact", None),
        "preference": ("preference", None),
        "failed": ("failure", None),
        "failure": ("failure", None),
        "lesson": ("lesson", None),
    }

    @staticmethod
    def _structured(event: Event) -> list[MemoryCandidate]:
        raw = event.payload.get("memory_candidates", event.payload.get("memory"))
        if raw is None:
            return []
        values = raw if isinstance(raw, list) else [raw]
        result: list[MemoryCandidate] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            memory_type = str(value.get("memory_type", value.get("type", ""))).casefold()
            content = str(value.get("content", "")).strip()
            block = value.get("block")
            block = str(block).casefold() if block is not None else None
            if memory_type not in _MEMORY_TYPES or not content:
                continue
            if block is not None and block not in _BLOCKS:
                continue
            result.append(
                MemoryCandidate(
                    memory_type=memory_type,
                    content=content,
                    summary=str(value.get("summary", "")).strip(),
                    block=block,
                    risk=float(value.get("risk", 0.0)),
                    retrieval_cost=float(value.get("retrieval_cost", 1.0)),
                    files=tuple(str(item) for item in value.get("files", event.files)),
                )
            )
        return result

    @classmethod
    def candidates(cls, event: Event) -> tuple[MemoryCandidate, ...]:
        policy = consolidation_policy(event)
        if not policy.allow_records:
            return ()
        result = cls._structured(event)
        for line in event.content.splitlines():
            match = _MARKER.match(line)
            if not match:
                continue
            memory_type, block = cls.MARKER_MAP[match.group(1).casefold()]
            result.append(
                MemoryCandidate(
                    memory_type=memory_type,
                    content=match.group(2),
                    block=block,
                    files=event.files,
                )
            )
        unique: dict[tuple[str, str, str | None], MemoryCandidate] = {}
        for item in result:
            if item.block is not None and not policy.allow_blocks:
                item = MemoryCandidate(
                    memory_type=item.memory_type,
                    content=item.content,
                    summary=item.summary,
                    block=None,
                    risk=item.risk,
                    retrieval_cost=item.retrieval_cost,
                    files=item.files,
                )
            unique[(item.memory_type, item.content.casefold(), item.block)] = item
        return tuple(unique.values())

    @staticmethod
    def _merge_block(current: str, content: str, *, replace: bool) -> str:
        if replace:
            return content
        existing = [line.strip().removeprefix("- ").strip() for line in current.splitlines()]
        if content.casefold() in {line.casefold() for line in existing if line}:
            return current
        return "\n".join(item for item in (current.rstrip(), f"- {content}") if item)

    def consolidate_event(
        self,
        service: MemoryService,
        event: Event,
    ) -> ConsolidationResult:
        previous = service.store.consolidation_result(event.id)
        if previous is not None:
            return ConsolidationResult(
                event_id=event.id,
                memory_ids=tuple(previous.get("memory_ids", ())),
                block_ids=tuple(previous.get("block_ids", ())),
                skipped_blocks=tuple(previous.get("skipped_blocks", ())),
            )

        memory_ids: list[str] = []
        block_ids: list[str] = []
        skipped: list[str] = []
        existing_records = service.store.list_memories(
            branch_id=event.branch_id,
            include_superseded=True,
        )
        for candidate in self.candidates(event):
            record = None
            if (event.role or "").casefold() == "user":
                matched = service.store.find_auto_candidate_match(
                    candidate.memory_type, candidate.content
                )
                if matched is not None:
                    candidate_id, memory_id = matched
                    service.store.confirm_candidate_match(
                        candidate_id,
                        memory_id,
                        source_event_id=event.id,
                        origin_evidence_type="host_logical_user",
                    )
                    record = service.store.get_memory(memory_id)
            record = record or next(
                (
                    item for item in existing_records
                    if item.memory_type == candidate.memory_type
                    and item.content == candidate.content
                    and item.source_event_ids == (event.id,)
                ),
                None,
            )
            if record is None:
                record = service.derive_memory(
                    memory_type=candidate.memory_type,
                    content=candidate.content,
                    summary=candidate.summary,
                    source_event_ids=(event.id,),
                    files=candidate.files,
                    branch_id=event.branch_id,
                    risk=candidate.risk,
                    retrieval_cost=candidate.retrieval_cost,
                    metadata={
                        "origin": "explicit_marker",
                        "authority_level": (
                            "confirmed"
                            if (event.role or "").casefold() == "user"
                            else "auto"
                        ),
                        "origin_evidence_type": (
                            "host_logical_user"
                            if (event.role or "").casefold() == "user"
                            else "extractor"
                        ),
                    },
                )
                existing_records.append(record)
            memory_ids.append(record.id)
            if candidate.block is None:
                continue
            current = service.store.get_active_blocks(branch_id=event.branch_id).get(candidate.block)
            content = self._merge_block(
                current.content if current else "",
                candidate.content,
                replace=candidate.block in {"goal", "instructions"},
            )
            if current is not None and content == current.content:
                continue
            try:
                block = service.store.set_active_block(
                    candidate.block,
                    content,
                    branch_id=event.branch_id,
                    session_id=event.session_id,
                    source_event_ids=(event.id,),
                )
            except ValueError as exc:
                if "protected active memory exceeds" not in str(exc):
                    raise
                skipped.append(candidate.block)
            else:
                block_ids.append(block.id)
        result = ConsolidationResult(
            event_id=event.id,
            memory_ids=tuple(memory_ids),
            block_ids=tuple(block_ids),
            skipped_blocks=tuple(skipped),
        )
        service.store.mark_consolidated(event.id, asdict(result))
        return result

    def consolidate_pending(
        self,
        service: MemoryService,
        *,
        branch_id: str = "main",
        events: Sequence[Event] | None = None,
    ) -> tuple[ConsolidationResult, ...]:
        selected = events or service.store.query_events(branch_id=branch_id)
        return tuple(self.consolidate_event(service, event) for event in selected)

    @staticmethod
    def _extractive_text(events: Sequence[Event], token_budget: int) -> str:
        header = "Extractive continuation summary. Every line is copied from canonical evidence."
        lines = [header]
        for event in events:
            compact = " ".join(event.content.split())
            candidate = f"- [{event.id}] {event.role or event.kind}: {compact}"
            if conservative_token_estimate("\n".join([*lines, candidate])) > token_budget:
                break
            lines.append(candidate)
        return "\n".join(lines)

    def compact(
        self,
        service: MemoryService,
        *,
        branch_id: str = "main",
        keep_recent_groups: int = 8,
        summary_budget: int = 600,
    ) -> CompactionResult:
        groups = interaction_groups(service.store.query_events(branch_id=branch_id))
        older = groups[:-keep_recent_groups] if keep_recent_groups else groups
        events = [event for group in older for event in group]
        if not events:
            return CompactionResult(None, None, (), "")
        text = self._extractive_text(events, summary_budget)
        included = tuple(
            event for event in events if f"[{event.id}]" in text
        )
        source_ids = tuple(event.id for event in included)
        if not source_ids:
            return CompactionResult(None, None, (), "")
        existing = service.store.list_memories(branch_id=branch_id)
        for record in existing:
            if record.memory_type == "summary" and record.source_event_ids == source_ids:
                matching_index = next(
                    (
                        item for item in existing
                        if item.memory_type == "index" and item.source_event_ids == source_ids
                    ),
                    None,
                )
                return CompactionResult(record, matching_index, source_ids, record.content)
        summary = service.derive_memory(
            memory_type="summary",
            content=text,
            summary=f"Continuation summary for {len(source_ids)} canonical events",
            source_event_ids=source_ids,
            branch_id=branch_id,
            retrieval_cost=0.25,
        )
        index = service.derive_memory(
            memory_type="index",
            content="\n".join(
                f"{event.created_at} {event.id} {event.kind} {' '.join(event.files)}"
                for event in included
            ),
            summary=f"Timeline index for {len(source_ids)} canonical events",
            source_event_ids=source_ids,
            branch_id=branch_id,
            retrieval_cost=0.1,
        )
        return CompactionResult(summary, index, source_ids, text)
