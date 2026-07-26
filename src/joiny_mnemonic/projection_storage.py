from __future__ import annotations

import json
from typing import Any

from .storage_support import json_text, now


class ProjectionStorageMixin:
    """SQLite owner for rebuildable retrieval and file-hash projections."""

    def retrieval_health_load(self) -> dict[str, dict[str, Any]]:
        """Rebuildable channel-health projection (see schema comment)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, payload_json FROM retrieval_channel_health"
            ).fetchall()
        return {
            str(row["channel"]): json.loads(row["payload_json"]) for row in rows
        }

    def retrieval_health_store(self, channels: dict[str, dict[str, Any]]) -> None:
        if not channels:
            return
        with self._transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO retrieval_channel_health"
                "(channel, payload_json, updated_at) VALUES(?,?,?)",
                [
                    (channel, json_text(payload), now())
                    for channel, payload in channels.items()
                ],
            )

    def file_hash_cache_load(self, root: str) -> dict[str, tuple[int, int, str]]:
        """Rebuildable stat->hash projection for one project root."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, size, mtime_ns, sha256 FROM file_hash_cache "
                "WHERE root=?",
                (root,),
            ).fetchall()
        return {
            str(row["path"]): (
                int(row["size"]), int(row["mtime_ns"]), str(row["sha256"])
            )
            for row in rows
        }

    def file_hash_cache_store(
        self, root: str, entries: dict[str, tuple[int, int, str]]
    ) -> None:
        if not entries:
            return
        with self._transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO file_hash_cache"
                "(root, path, size, mtime_ns, sha256) VALUES(?,?,?,?,?)",
                [
                    (root, path, size, mtime_ns, sha256)
                    for path, (size, mtime_ns, sha256) in entries.items()
                ],
            )
