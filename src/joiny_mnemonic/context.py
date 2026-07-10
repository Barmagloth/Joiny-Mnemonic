from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import Event
from .transcript import TRANSCRIPT_KINDS, interaction_groups

if TYPE_CHECKING:
    from .storage import MemoryStore


MAX_CONTEXT_GROUPS = 20


@dataclass(frozen=True, slots=True)
class ContextIndexEntry:
    group: int
    seq: int
    id: str
    branch_id: str
    session_id: str | None
    kind: str
    role: str | None
    preview: str
    files: tuple[str, ...]
    is_source: bool
    is_primary: bool


@dataclass(frozen=True, slots=True)
class ContextWindow:
    id: str
    branch_id: str
    primary_event_id: str
    source_event_ids: tuple[str, ...]
    before: int
    after: int
    include_source: bool
    group_count: int
    index: tuple[ContextIndexEntry, ...] = ()
    events: tuple[Event, ...] = ()


@dataclass(frozen=True, slots=True)
class ExactSourceResult:
    id: str
    source_event_ids: tuple[str, ...]
    events: tuple[Event, ...]


def build_context_window(
    store: MemoryStore,
    identifier: str,
    source_events: tuple[Event, ...],
    *,
    branch_id: str,
    before: int,
    after: int,
    include_source: bool,
) -> ContextWindow:
    for name, value in (("before", before), ("after", after)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if not 0 <= value <= MAX_CONTEXT_GROUPS:
            raise ValueError(f"{name} must be between 0 and {MAX_CONTEXT_GROUPS}")
    if not source_events:
        raise ValueError("context expansion requires at least one source event")

    lineage = dict(store.branch_lineage(branch_id))
    invisible = [
        event.id
        for event in source_events
        if event.branch_id not in lineage
        or (
            lineage[event.branch_id] is not None
            and event.seq > int(lineage[event.branch_id])
        )
    ]
    if invisible:
        raise ValueError(
            "source events are outside the requested branch lineage: "
            + ", ".join(sorted(invisible))
        )

    visible_events = store.query_events(branch_id=branch_id)
    transcript_groups = interaction_groups(visible_events)
    groups = [
        *transcript_groups,
        *(
            [event]
            for event in visible_events
            if event.kind not in TRANSCRIPT_KINDS
        ),
    ]
    groups.sort(key=lambda group: min(event.seq for event in group))

    source_ids = {event.id for event in source_events}
    source_group_indexes = {
        index
        for index, group in enumerate(groups)
        if any(event.id in source_ids for event in group)
    }
    if not source_group_indexes:
        raise ValueError("source event has no complete interaction group")

    selected_indexes: set[int] = set()
    for index in source_group_indexes:
        selected_indexes.update(
            range(max(0, index - before), min(len(groups), index + after + 1))
        )
    selected_groups = [groups[index] for index in sorted(selected_indexes)]
    selected_events = tuple(
        sorted(
            (event for group in selected_groups for event in group),
            key=lambda event: event.seq,
        )
    )
    primary = min(source_events, key=lambda event: event.seq)
    if include_source:
        index_entries: tuple[ContextIndexEntry, ...] = ()
        exact_events = selected_events
    else:
        group_by_event = {
            event.id: group_index
            for group_index, group in enumerate(selected_groups)
            for event in group
        }
        index_entries = tuple(
            ContextIndexEntry(
                group=group_by_event[event.id],
                seq=event.seq,
                id=event.id,
                branch_id=event.branch_id,
                session_id=event.session_id,
                kind=event.kind,
                role=event.role,
                preview=" ".join(event.content.split())[:160],
                files=event.files,
                is_source=event.id in source_ids,
                is_primary=event.id == primary.id,
            )
            for event in selected_events
        )
        exact_events = ()
    return ContextWindow(
        id=identifier,
        branch_id=branch_id,
        primary_event_id=primary.id,
        source_event_ids=tuple(event.id for event in source_events),
        before=before,
        after=after,
        include_source=include_source,
        group_count=len(selected_groups),
        index=index_entries,
        events=exact_events,
    )
