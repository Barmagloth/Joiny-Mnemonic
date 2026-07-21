from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .provenance import HOST_ASSISTANT_FINALIZATION, origin_evidence_type


FINALIZATION_PATTERN = re.compile(
    r"^\[(GOAL|DECISION|FACT|CONSTRAINT|TODO|PREFERENCE|FAILURE|LESSON)\] "
    r"(CONFIRMED|REJECTED|DEFERRED): (\S(?:.*\S)?)$"
)
_TAG_LOOKALIKE = re.compile(
    r"\[(?:GOAL|DECISION|FACT|CONSTRAINT|TODO|PREFERENCE|FAILURE|LESSON)\]"
)
_BACKTICK_FENCE = chr(96) * 3
_MAX_TEXT_LENGTH = 2000


def classify_finalization_text(content: str) -> dict[str, Any]:
    """Classify strict standalone tags without interpreting their semantics."""
    valid: list[dict[str, str]] = []
    malformed: list[str] = []
    excluded: list[str] = []
    fence: str | None = None

    for line in content.splitlines():
        stripped = line.lstrip()
        marker = (
            stripped[:3]
            if stripped.startswith(_BACKTICK_FENCE) or stripped.startswith("~~~")
            else None
        )
        if marker is not None:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if not _TAG_LOOKALIKE.search(line):
            continue
        if fence is not None or stripped.startswith(">"):
            excluded.append(line)
            continue
        match = FINALIZATION_PATTERN.fullmatch(line)
        if match is None or len(match.group(3)) > _MAX_TEXT_LENGTH:
            malformed.append(line)
            continue
        valid.append({
            "type": match.group(1),
            "status": match.group(2),
            "text": match.group(3),
        })

    return {
        "valid": valid,
        "malformed": malformed,
        "excluded": excluded,
    }


def _read_stop_events(database: Path) -> list[sqlite3.Row]:
    if not database.is_file():
        raise FileNotFoundError(f"memory database does not exist: {database}")
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT id,seq,branch_id,role,origin_channel,origin_adapter,"
            "payload_json,content FROM events "
            "WHERE origin_channel='host_hook' AND lower(coalesce(role,''))='assistant' "
            "ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()


def observe_finalizations(
    database: str | Path,
    *,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Return read-only grammar statistics over captured assistant Stop events."""
    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")
    rows = _read_stop_events(Path(database))
    eligible: list[sqlite3.Row] = []
    ineligible_ids: list[str] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        event = SimpleNamespace(
            role=row["role"],
            origin_channel=row["origin_channel"],
            origin_adapter=row["origin_adapter"],
            payload=payload,
        )
        if payload.get("hook_event_name") != "Stop":
            continue
        if origin_evidence_type(event) == HOST_ASSISTANT_FINALIZATION:
            eligible.append(row)
        else:
            ineligible_ids.append(str(row["id"]))

    by_type: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    by_adapter: dict[str, Counter[str]] = {}
    valid_ids: list[str] = []
    untagged_ids: list[str] = []
    malformed_ids: list[str] = []
    excluded_ids: list[str] = []
    valid_tag_count = 0

    for row in eligible:
        result = classify_finalization_text(str(row["content"]))
        tags = result["valid"]
        adapter = str(row["origin_adapter"])
        adapter_counts = by_adapter.setdefault(adapter, Counter())
        adapter_counts["stop_events"] += 1
        adapter_counts["valid_tags"] += len(tags)
        valid_tag_count += len(tags)
        for tag in tags:
            by_type[tag["type"]] += 1
            by_status[tag["status"]] += 1
        event_id = str(row["id"])
        if tags:
            valid_ids.append(event_id)
            adapter_counts["events_with_valid_tags"] += 1
        else:
            untagged_ids.append(event_id)
            adapter_counts["events_without_valid_tags"] += 1
        if result["malformed"]:
            malformed_ids.append(event_id)
            adapter_counts["events_with_malformed_lookalikes"] += 1
        if result["excluded"]:
            excluded_ids.append(event_id)
            adapter_counts["events_with_excluded_lookalikes"] += 1

    def sampled(values: list[str]) -> list[str]:
        return values[:sample_limit]

    return {
        "database": str(Path(database).resolve()),
        "host_assistant_stop_events": len(eligible),
        "ineligible_stop_events": len(ineligible_ids),
        "events_with_valid_tags": len(valid_ids),
        "events_without_valid_tags": len(untagged_ids),
        "events_with_malformed_lookalikes": len(malformed_ids),
        "events_with_excluded_lookalikes": len(excluded_ids),
        "valid_tag_count": valid_tag_count,
        "by_type": dict(sorted(by_type.items())),
        "by_status": dict(sorted(by_status.items())),
        "by_adapter": {
            adapter: dict(sorted(counts.items()))
            for adapter, counts in sorted(by_adapter.items())
        },
        "event_ids": {
            "valid": sampled(valid_ids),
            "untagged": sampled(untagged_ids),
            "malformed": sampled(malformed_ids),
            "excluded": sampled(excluded_ids),
            "ineligible": sampled(ineligible_ids),
        },
        "sample_limit": sample_limit,
        "observation_only": True,
        "materialized": False,
    }
