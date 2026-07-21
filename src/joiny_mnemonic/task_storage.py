from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from typing import Any

from .models import TaskRecord
from .storage_support import integrity_checked, json_text, now
from .transition_rules import WORKSTREAM_RULE, validate_transition


class TaskStorageMixin:
    def _event_visible_locked(
        self, conn: sqlite3.Connection, event_row: sqlite3.Row, branch_id: str
    ) -> bool:
        event_branch = str(event_row["branch_id"])
        event_seq = int(event_row["seq"])
        return any(
            visible_branch == event_branch
            and (cutoff is None or event_seq <= cutoff)
            for visible_branch, cutoff in self._lineage_locked(branch_id)
        )

    @staticmethod
    def _delegation_enabled_locked(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT policy_json FROM policy_ledger ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return bool(
            row is not None
            and json.loads(row["policy_json"]).get(
                "agent_settlement_delegation_enabled", False
            )
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"], task_key=row["task_key"], branch_id=row["branch_id"],
            version=int(row["version"]), title=row["title"], status=row["status"],
            parent_task_key=row["parent_task_key"],
            source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
            snapshot_id=row["snapshot_id"], metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )

    def create_task_version(
        self,
        *,
        task_key: str,
        branch_id: str,
        title: str,
        status: str,
        source_event_ids: Sequence[str],
        parent_task_key: str | None = None,
        snapshot_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        reopen: bool = False,
        transition_reason: str = "",
    ) -> TaskRecord:
        if not task_key or not title:
            raise ValueError("task key and title must be non-empty")
        if status not in {"active", "blocked", "completed", "cancelled"}:
            raise ValueError("unsupported task status")
        safe_title, _ = self.redactor.redact_text(title)
        safe_metadata, _ = self.redactor.redact_value(metadata or {})
        with self._transaction() as conn:
            self._assert_source_events(conn, source_event_ids, branch_id=branch_id)
            current = conn.execute(
                "SELECT * FROM task_versions WHERE task_key=? ORDER BY version DESC LIMIT 1",
                (task_key,),
            ).fetchone()
            if current is not None and current["branch_id"] != branch_id:
                raise ValueError("task key is already assigned to another branch")
            if current is not None and str(current["status"]) == status:
                return self._task_from_row(current)
            if current is None:
                if status != "active":
                    raise ValueError("a workstream must start in active state")
            else:
                source_id = str(source_event_ids[-1]) if source_event_ids else ""
                source_row = conn.execute(
                    "SELECT * FROM events WHERE id=?", (source_id,)
                ).fetchone()
                if source_row is None:
                    raise KeyError(f"unknown source event: {source_id}")
                previous_sources = set(json.loads(current["source_event_ids_json"]))
                if source_id in previous_sources:
                    raise ValueError("workstream transition requires a new source event")
                decision = validate_transition(
                    WORKSTREAM_RULE,
                    current=str(current["status"]),
                    target=status,
                    origin=self._event_origin_evidence(source_row),
                    source_visible=self._event_visible_locked(
                        conn, source_row, branch_id
                    ),
                    delegated_enabled=self._delegation_enabled_locked(conn),
                    reopen=reopen,
                    reason=transition_reason,
                )
                if not decision.changed:
                    return self._task_from_row(current)
            version = int(current["version"]) + 1 if current is not None else 1
            task_id = f"task_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO task_versions(id,task_key,branch_id,version,title,status,"
                "parent_task_key,source_event_ids_json,snapshot_id,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id, task_key, branch_id, version, safe_title, status,
                    parent_task_key, json_text(list(dict.fromkeys(source_event_ids))), snapshot_id,
                    json_text(safe_metadata), now(),
                ),
            )
            row = conn.execute("SELECT * FROM task_versions WHERE id=?", (task_id,)).fetchone()
        assert row is not None
        return self._task_from_row(row)

    @integrity_checked
    def get_task(self, task_key: str) -> TaskRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_versions WHERE task_key=? ORDER BY version DESC LIMIT 1",
                (task_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_key}")
        return self._task_from_row(row)

    @integrity_checked
    def list_tasks(self, *, status: str | None = None) -> tuple[TaskRecord, ...]:
        sql = (
            "SELECT t.* FROM task_versions t JOIN (SELECT task_key,MAX(version) version "
            "FROM task_versions GROUP BY task_key) latest "
            "ON latest.task_key=t.task_key AND latest.version=t.version"
        )
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE t.status=?"
            params = (status,)
        sql += " ORDER BY t.created_at"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return tuple(self._task_from_row(row) for row in rows)

    def bind_task_session(self, session_id: str, task_key: str) -> None:
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT task_key FROM task_session_bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing is not None:
                if existing["task_key"] != task_key:
                    raise ValueError("session is already bound to another task")
                return
            if conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                raise KeyError(f"unknown session: {session_id}")
            if conn.execute("SELECT 1 FROM task_versions WHERE task_key=?", (task_key,)).fetchone() is None:
                raise KeyError(f"unknown task: {task_key}")
            conn.execute(
                "INSERT INTO task_session_bindings(session_id,task_key,created_at) VALUES(?,?,?)",
                (session_id, task_key, now()),
            )

    @integrity_checked
    def has_hook_activity(self, agent: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM hook_sessions WHERE agent=? LIMIT 1",
                (agent,),
            ).fetchone()
        return row is not None

    @integrity_checked
    def task_for_hook_session(self, agent: str, external_session_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT t.* FROM hook_sessions h JOIN task_session_bindings b "
                "ON b.session_id=h.session_id JOIN task_versions t ON t.task_key=b.task_key "
                "WHERE h.agent=? AND h.external_session_id=? "
                "ORDER BY t.version DESC LIMIT 1",
                (agent, external_session_id),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None
