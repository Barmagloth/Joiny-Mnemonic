from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from .models import Event


TRANSCRIPT_KINDS = frozenset({"message", "tool_call", "tool_output", "artifact"})


def tool_call_id(event: Event) -> str | None:
    value = event.payload.get("_memory_call_id")
    if value is None:
        for key in ("tool_call_id", "tool_use_id", "call_id"):
            if event.payload.get(key) is not None:
                value = event.payload[key]
                break
    return str(value) if value is not None else None


def interaction_groups(events: Sequence[Event]) -> list[list[Event]]:
    """Return atomic transcript units, pairing every tool output with its call.

    Orphan outputs are intentionally omitted from resume context. Canonical history remains
    untouched and can still be inspected through exact-source retrieval.
    """
    transcript = [event for event in events if event.kind in TRANSCRIPT_KINDS]
    parents = list(range(len(transcript)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    calls_by_id: dict[str, list[int]] = defaultdict(list)
    unmatched_calls: list[int] = []
    matched_outputs: set[int] = set()
    consumed_calls: set[int] = set()
    for index, event in enumerate(transcript):
        if event.kind == "tool_call":
            correlation = tool_call_id(event)
            if correlation:
                calls_by_id[correlation].append(index)
            unmatched_calls.append(index)
            continue
        if event.kind != "tool_output":
            continue
        correlation = tool_call_id(event)
        call_index: int | None = None
        if correlation:
            candidates = calls_by_id.get(correlation, [])
            call_index = next(
                (candidate for candidate in reversed(candidates) if candidate not in consumed_calls),
                None,
            )
        if call_index is None:
            call_index = next(
                (candidate for candidate in reversed(unmatched_calls) if candidate not in consumed_calls),
                None,
            )
        if call_index is not None:
            consumed_calls.add(call_index)
            matched_outputs.add(index)
            union(call_index, index)

    grouped: dict[int, list[Event]] = defaultdict(list)
    for index, event in enumerate(transcript):
        if event.kind == "tool_output" and index not in matched_outputs:
            continue
        grouped[find(index)].append(event)
    groups = [sorted(group, key=lambda event: event.seq) for group in grouped.values()]
    return sorted(groups, key=lambda group: min(event.seq for event in group))


def recent_complete_events(events: Sequence[Event], group_limit: int) -> list[Event]:
    if group_limit <= 0:
        return []
    groups = interaction_groups(events)[-group_limit:]
    return sorted((event for group in groups for event in group), key=lambda event: event.seq)
