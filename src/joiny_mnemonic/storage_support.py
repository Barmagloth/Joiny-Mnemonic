from __future__ import annotations

import functools
import json
from datetime import UTC, datetime
from typing import Any


def integrity_checked(method: Any) -> Any:
    @functools.wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        self._guard_read()
        return method(self, *args, **kwargs)

    return wrapped


def atomic_write(method: Any) -> Any:
    @functools.wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._transaction():
            return method(self, *args, **kwargs)

    return wrapped


def store_read(method: Any) -> Any:
    """Declare a public MemoryStore operation as read-only for surface audits."""
    method._joiny_store_read_only = True
    return method


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def common_event_trace(
    connection: Any, source_event_ids: tuple[str, ...]
) -> tuple[str | None, str | None]:
    """Return saved trace fields only when every source agrees on them."""
    if not source_event_ids:
        return None, None
    placeholders = ",".join("?" for _ in source_event_ids)
    rows = connection.execute(
        "SELECT session_id,origin_adapter FROM events WHERE id IN ("
        + placeholders + ")",
        source_event_ids,
    ).fetchall()
    sessions = {row["session_id"] for row in rows}
    adapters = {row["origin_adapter"] for row in rows}
    return (
        next(iter(sessions)) if len(sessions) == 1 else None,
        next(iter(adapters)) if len(adapters) == 1 else None,
    )
