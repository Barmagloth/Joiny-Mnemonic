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
