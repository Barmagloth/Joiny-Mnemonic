from __future__ import annotations

import uuid
from typing import Any

from .provenance import HOST_ASSISTANT_FINALIZATION, origin_evidence_type
from .storage_errors import StoreIntegrityError
from .storage_support import integrity_checked, now, store_read


FINALIZATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS finalization_records (
    id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL REFERENCES events(id),
    branch_id TEXT NOT NULL REFERENCES branches(id),
    finalization_type TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    memory_id TEXT REFERENCES memory_records(id),
    created_at TEXT NOT NULL,
    UNIQUE(source_event_id, finalization_type, status, content)
);
CREATE TABLE IF NOT EXISTS finalization_quarantine (
    id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL REFERENCES events(id),
    reason_code TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_event_id, reason_code, raw_content)
);
CREATE TRIGGER IF NOT EXISTS finalization_records_no_update
BEFORE UPDATE ON finalization_records
BEGIN SELECT RAISE(ABORT, 'finalization records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS finalization_records_no_delete
BEFORE DELETE ON finalization_records
BEGIN SELECT RAISE(ABORT, 'finalization records cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS finalization_quarantine_no_update
BEFORE UPDATE ON finalization_quarantine
BEGIN SELECT RAISE(ABORT, 'finalization quarantine is immutable'); END;
CREATE TRIGGER IF NOT EXISTS finalization_quarantine_no_delete
BEFORE DELETE ON finalization_quarantine
BEGIN SELECT RAISE(ABORT, 'finalization quarantine cannot be deleted'); END;
"""


class FinalizationStorageMixin:
    def _migrate_to_v11(self) -> None:
        # BASE_SCHEMA creates the append-only finalization audit tables.
        return None

    @integrity_checked
    def record_finalization(
        self,
        *,
        source_event_id: str,
        branch_id: str,
        finalization_type: str,
        status: str,
        content: str,
        memory_id: str | None = None,
    ) -> str:
        """Append one deterministic finalization audit row, idempotently."""
        with self._transaction() as conn:
            source = conn.execute(
                "SELECT * FROM events WHERE id=?", (source_event_id,)
            ).fetchone()
            if source is None:
                raise KeyError(f"unknown source event: {source_event_id}")
            if str(source["branch_id"]) != branch_id:
                raise ValueError("finalization source is outside the target branch")
            if (
                origin_evidence_type(self._event_from_row(source))
                != HOST_ASSISTANT_FINALIZATION
            ):
                raise ValueError("finalization requires a trusted assistant Stop event")
            if memory_id is not None:
                memory = conn.execute(
                    "SELECT branch_id FROM memory_records WHERE id=?", (memory_id,)
                ).fetchone()
                if memory is None:
                    raise KeyError(f"unknown finalization memory: {memory_id}")
                if str(memory["branch_id"]) != branch_id:
                    raise ValueError("finalization memory is outside the target branch")
            existing = conn.execute(
                "SELECT id,memory_id FROM finalization_records WHERE "
                "source_event_id=? AND finalization_type=? AND status=? AND content=?",
                (source_event_id, finalization_type, status, content),
            ).fetchone()
            if existing is not None:
                if existing["memory_id"] != memory_id:
                    raise StoreIntegrityError("finalization replay changed its memory link")
                return str(existing["id"])
            record_id = f"fin_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO finalization_records"
                "(id,source_event_id,branch_id,finalization_type,status,content,"
                "memory_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    record_id, source_event_id, branch_id, finalization_type,
                    status, content, memory_id, now(),
                ),
            )
        return record_id

    @integrity_checked
    def quarantine_finalization(
        self, *, source_event_id: str, reason_code: str, raw_content: str
    ) -> str:
        """Append a fail-closed finalization rejection with a stable reason."""
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM events WHERE id=?", (source_event_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown source event: {source_event_id}")
            existing = conn.execute(
                "SELECT id FROM finalization_quarantine WHERE "
                "source_event_id=? AND reason_code=? AND raw_content=?",
                (source_event_id, reason_code, raw_content),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            quarantine_id = f"finq_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO finalization_quarantine"
                "(id,source_event_id,reason_code,raw_content,created_at) "
                "VALUES(?,?,?,?,?)",
                (quarantine_id, source_event_id, reason_code, raw_content, now()),
            )
        return quarantine_id

    @store_read
    @integrity_checked
    def list_finalizations(
        self, *, branch_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if branch_id is not None:
            clauses.append("branch_id=?")
            params.append(branch_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._conn.execute(
            "SELECT * FROM finalization_records" + where + " ORDER BY rowid", params
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def assert_agent_finalized_authority(
        self, conn: Any, metadata: dict[str, Any], source_event_ids: tuple[str, ...]
    ) -> None:
        if metadata.get("authority_level") != "agent_finalized":
            return
        if len(source_event_ids) != 1:
            raise ValueError("agent_finalized memory requires one exact source event")
        row = conn.execute(
            "SELECT * FROM events WHERE id=?", (source_event_ids[0],)
        ).fetchone()
        if row is None or origin_evidence_type(
            self._event_from_row(row)
        ) != HOST_ASSISTANT_FINALIZATION:
            raise ValueError(
                "agent_finalized authority requires a trusted assistant Stop event"
            )

    @store_read
    @integrity_checked
    def list_finalization_quarantine(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM finalization_quarantine ORDER BY rowid"
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    @integrity_checked
    def automatic_memory_eligible(self, memory_id: str) -> bool:
        """Return whether a record may enter automatic retrieval/resume."""
        record = self.get_memory(memory_id)
        if (
            record.metadata.get("origin") == "host_finalization"
            and record.metadata.get("authority_level") == "agent_finalized"
        ):
            return True
        if not record.source_event_ids:
            return True
        placeholders = ",".join("?" for _ in record.source_event_ids)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE id IN (" + placeholders + ") "
                "AND kind='message' AND lower(coalesce(role,''))='assistant' LIMIT 1",
                record.source_event_ids,
            ).fetchone()
        return row is None
