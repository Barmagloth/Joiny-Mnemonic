from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from .storage_support import integrity_checked


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class DataflowStorageMixin:
    """SQLite adapter for the append-only dataflow projection."""

    @staticmethod
    def _dataflow_from_row(row: Any) -> dict[str, Any]:
        return {
            "seq": int(row["seq"]),
            "id": row["id"],
            "operation_id": row["operation_id"],
            "parent_operation_id": row["parent_operation_id"],
            "operation_name": row["operation_name"],
            "branch_id": row["branch_id"],
            "session_id": row["session_id"],
            "source": row["source"],
            "stage": row["stage"],
            "status": row["status"],
            "input": json.loads(row["input_json"]),
            "output": json.loads(row["output_json"]),
            "refs": json.loads(row["refs_json"]),
            "decision": json.loads(row["decision_json"]),
            "error": json.loads(row["error_json"]),
            "duration_ms": (
                float(row["duration_ms"]) if row["duration_ms"] is not None else None
            ),
            "created_at": row["created_at"],
        }

    def record_dataflow_entry(self, **item: Any) -> dict[str, Any]:
        required = ("operation_id", "operation_name", "source", "stage", "status")
        if any(not item.get(key) for key in required):
            raise ValueError("dataflow operation, source, stage and status are required")
        if item["status"] not in {"started", "completed", "failed", "skipped"}:
            raise ValueError("invalid dataflow status")
        payloads = [
            self.redactor.redact_value(item.get(key, {}))[0]
            for key in ("input_value", "output_value", "refs", "decision", "error")
        ]
        entry_id = f"flow_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO dataflow_entries("
                "id,operation_id,parent_operation_id,operation_name,branch_id,session_id,"
                "source,stage,status,input_json,output_json,refs_json,decision_json,"
                "error_json,duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id, item["operation_id"], item.get("parent_operation_id"),
                    item["operation_name"], item.get("branch_id", "main"),
                    item.get("session_id"), item["source"], item["stage"], item["status"],
                    *(_json(value) for value in payloads), item.get("duration_ms"), _now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM dataflow_entries WHERE id=?", (entry_id,)
            ).fetchone()
        assert row is not None
        return self._dataflow_from_row(row)

    @integrity_checked
    def list_dataflow_entries(
        self, *, branch_id: str | None = None, after_seq: int = 0, limit: int = 500
    ) -> tuple[dict[str, Any], ...]:
        if limit < 1 or limit > 5000:
            raise ValueError("dataflow limit must be between 1 and 5000")
        clauses, params = ["seq>?"], [int(after_seq)]
        if branch_id is not None:
            clauses.append("branch_id=?")
            params.append(branch_id)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dataflow_entries WHERE "
                + " AND ".join(clauses) + " ORDER BY seq LIMIT ?",
                params,
            ).fetchall()
        return tuple(self._dataflow_from_row(row) for row in rows)

    @integrity_checked
    def get_dataflow_operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dataflow_entries WHERE operation_id=? ORDER BY seq",
                (operation_id,),
            ).fetchall()
        if not rows:
            raise KeyError(f"unknown dataflow operation: {operation_id}")
        entries = [self._dataflow_from_row(row) for row in rows]
        return self._summarize_dataflow(entries, include_entries=True)

    @staticmethod
    def _summarize_dataflow(
        entries: list[dict[str, Any]], *, include_entries: bool = False
    ) -> dict[str, Any]:
        first = entries[0]
        terminal = next(
            (
                entry for entry in reversed(entries)
                if entry["stage"] == "operation"
                and entry["status"] in {"completed", "failed", "skipped"}
            ),
            entries[-1],
        )
        result = {
            "operation_id": first["operation_id"],
            "parent_operation_id": first["parent_operation_id"],
            "operation_name": first["operation_name"],
            "branch_id": terminal["branch_id"],
            "session_id": terminal["session_id"] or first["session_id"],
            "source": first["source"],
            "status": terminal["status"],
            "started_at": first["created_at"],
            "finished_at": terminal["created_at"],
            "entry_count": len(entries),
            "last_seq": entries[-1]["seq"],
        }
        if include_entries:
            result["entries"] = entries
        return result

    @integrity_checked
    def list_dataflow_operations(
        self, *, branch_id: str | None = None, limit: int = 50
    ) -> tuple[dict[str, Any], ...]:
        if limit < 1 or limit > 500:
            raise ValueError("operation limit must be between 1 and 500")
        where, params = "", []
        if branch_id is not None:
            where, params = "WHERE branch_id=?", [branch_id]
        params.append(limit)
        with self._lock:
            ids = self._conn.execute(
                "SELECT operation_id,MAX(seq) AS last_seq FROM dataflow_entries "
                f"{where} GROUP BY operation_id ORDER BY last_seq DESC LIMIT ?",
                params,
            ).fetchall()
            result = []
            for item in ids:
                rows = self._conn.execute(
                    "SELECT * FROM dataflow_entries WHERE operation_id=? ORDER BY seq",
                    (item["operation_id"],),
                ).fetchall()
                entries = [self._dataflow_from_row(row) for row in rows]
                result.append(self._summarize_dataflow(entries))
        return tuple(result)
