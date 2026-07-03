from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import (
    ActiveBlock, Artifact, BudgetPolicy, Event, MemoryRecord, Snapshot, TaskRecord,
    ToolOutputView, UsageSample,
)
from .security import SecretRedactor


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _fts_match_query(value: str) -> str:
    terms = [term for term in re.findall(r"[\w./:-]+", value, re.UNICODE) if term]
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


class StoreIntegrityError(RuntimeError):
    """Raised when canonical storage fails automatic integrity verification."""


def integrity_checked(method: Any) -> Any:
    @functools.wraps(method)
    def wrapped(self: "MemoryStore", *args: Any, **kwargs: Any) -> Any:
        self._guard_read()
        return method(self, *args, **kwargs)

    return wrapped


BASE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES branches(id),
    fork_event_seq INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    agent TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hook_sessions (
    agent TEXT NOT NULL,
    external_session_id TEXT NOT NULL,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(agent, external_session_id)
);

CREATE TABLE IF NOT EXISTS ingest_receipts (
    receipt_key TEXT PRIMARY KEY,
    event_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consolidation_receipts (
    event_id TEXT PRIMARY KEY REFERENCES events(id),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    session_id TEXT REFERENCES sessions(id),
    kind TEXT NOT NULL,
    role TEXT,
    content TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    files_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    previous_hash TEXT,
    content_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    data BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS block_versions (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    supersedes_id TEXT REFERENCES block_versions(id),
    cursor_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(branch_id, name, version)
);

CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL,
    files_json TEXT NOT NULL,
    risk REAL NOT NULL,
    retrieval_cost REAL NOT NULL,
    version INTEGER NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    supersedes_id TEXT REFERENCES memory_records(id),
    cursor_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_output_views (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    level TEXT NOT NULL,
    reducer TEXT NOT NULL,
    reducer_version TEXT NOT NULL,
    content TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_bytes INTEGER NOT NULL,
    view_bytes INTEGER NOT NULL,
    raw_tokens INTEGER NOT NULL,
    view_tokens INTEGER NOT NULL,
    latency_ns INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(event_id, level, reducer, reducer_version)
);

CREATE TABLE IF NOT EXISTS usage_samples (
    id TEXT PRIMARY KEY,
    receipt_key TEXT UNIQUE,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    session_id TEXT REFERENCES sessions(id),
    event_id TEXT REFERENCES events(id),
    source TEXT NOT NULL,
    operation TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    context_tokens INTEGER NOT NULL,
    estimated INTEGER NOT NULL,
    cost_usd REAL,
    latency_ms REAL,
    raw_bytes INTEGER NOT NULL,
    emitted_bytes INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hook_context_counters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_key TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    event_id TEXT REFERENCES events(id),
    event_name TEXT NOT NULL,
    increment_tokens INTEGER NOT NULL,
    cumulative_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_policies (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    version INTEGER NOT NULL,
    context_window_tokens INTEGER NOT NULL,
    snapshot_ratio REAL NOT NULL,
    compact_ratio REAL NOT NULL,
    handoff_ratio REAL NOT NULL,
    hard_limit_ratio REAL NOT NULL,
    min_action_interval_events INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(branch_id, version)
);

CREATE TABLE IF NOT EXISTS governor_actions (
    id TEXT PRIMARY KEY,
    receipt_key TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    session_id TEXT REFERENCES sessions(id),
    source_event_id TEXT REFERENCES events(id),
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    context_tokens INTEGER NOT NULL,
    threshold_tokens INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_versions (
    id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_task_key TEXT,
    source_event_ids_json TEXT NOT NULL,
    snapshot_id TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_key, version)
);

CREATE TABLE IF NOT EXISTS task_session_bindings (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    task_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    parent_snapshot_id TEXT REFERENCES snapshots(id),
    cursor_seq INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    project_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_branch_seq ON events(branch_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_branch_type ON memory_records(branch_id, memory_type);
CREATE INDEX IF NOT EXISTS idx_snapshots_branch_cursor ON snapshots(branch_id, cursor_seq);
CREATE INDEX IF NOT EXISTS idx_tool_views_event_level ON tool_output_views(event_id, level);
CREATE INDEX IF NOT EXISTS idx_usage_branch_created ON usage_samples(branch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_session_created ON usage_samples(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_hook_context_session_id ON hook_context_counters(session_id, id);
CREATE INDEX IF NOT EXISTS idx_governor_branch_created ON governor_actions(branch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_key_version ON task_versions(task_key, version);
CREATE INDEX IF NOT EXISTS idx_task_branch_created ON task_versions(branch_id, created_at);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'canonical events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'canonical events cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS artifacts_no_update BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT, 'canonical artifacts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifacts_no_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT, 'canonical artifacts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS blocks_no_update BEFORE UPDATE ON block_versions
BEGIN SELECT RAISE(ABORT, 'blocks are versioned, not updated'); END;
CREATE TRIGGER IF NOT EXISTS blocks_no_delete BEFORE DELETE ON block_versions
BEGIN SELECT RAISE(ABORT, 'block versions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS memories_no_update BEFORE UPDATE ON memory_records
BEGIN SELECT RAISE(ABORT, 'memories are versioned, not updated'); END;
CREATE TRIGGER IF NOT EXISTS memories_no_delete BEFORE DELETE ON memory_records
BEGIN SELECT RAISE(ABORT, 'memory versions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS tool_views_no_update BEFORE UPDATE ON tool_output_views
BEGIN SELECT RAISE(ABORT, 'tool output views are versioned, not updated'); END;
CREATE TRIGGER IF NOT EXISTS tool_views_no_delete BEFORE DELETE ON tool_output_views
BEGIN SELECT RAISE(ABORT, 'tool output views cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS usage_no_update BEFORE UPDATE ON usage_samples
BEGIN SELECT RAISE(ABORT, 'usage samples are append-only'); END;
CREATE TRIGGER IF NOT EXISTS usage_no_delete BEFORE DELETE ON usage_samples
BEGIN SELECT RAISE(ABORT, 'usage samples cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS hook_context_no_update BEFORE UPDATE ON hook_context_counters
BEGIN SELECT RAISE(ABORT, 'hook context counters are append-only'); END;
CREATE TRIGGER IF NOT EXISTS hook_context_no_delete BEFORE DELETE ON hook_context_counters
BEGIN SELECT RAISE(ABORT, 'hook context counters cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS budget_policies_no_update BEFORE UPDATE ON budget_policies
BEGIN SELECT RAISE(ABORT, 'budget policies are versioned, not updated'); END;
CREATE TRIGGER IF NOT EXISTS budget_policies_no_delete BEFORE DELETE ON budget_policies
BEGIN SELECT RAISE(ABORT, 'budget policy versions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS governor_actions_no_update BEFORE UPDATE ON governor_actions
BEGIN SELECT RAISE(ABORT, 'governor actions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS governor_actions_no_delete BEFORE DELETE ON governor_actions
BEGIN SELECT RAISE(ABORT, 'governor actions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS task_versions_no_update BEFORE UPDATE ON task_versions
BEGIN SELECT RAISE(ABORT, 'task records are versioned, not updated'); END;
CREATE TRIGGER IF NOT EXISTS task_versions_no_delete BEFORE DELETE ON task_versions
BEGIN SELECT RAISE(ABORT, 'task versions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS task_bindings_no_update BEFORE UPDATE ON task_session_bindings
BEGIN SELECT RAISE(ABORT, 'task session bindings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS task_bindings_no_delete BEFORE DELETE ON task_session_bindings
BEGIN SELECT RAISE(ABORT, 'task session bindings cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS branches_no_update BEFORE UPDATE ON branches
BEGIN SELECT RAISE(ABORT, 'branch lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS branches_no_delete BEFORE DELETE ON branches
BEGIN SELECT RAISE(ABORT, 'branch lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS sessions_no_update BEFORE UPDATE ON sessions
BEGIN SELECT RAISE(ABORT, 'session metadata is immutable'); END;
CREATE TRIGGER IF NOT EXISTS sessions_no_delete BEFORE DELETE ON sessions
BEGIN SELECT RAISE(ABORT, 'session metadata is immutable'); END;
CREATE TRIGGER IF NOT EXISTS hook_sessions_no_update BEFORE UPDATE ON hook_sessions
BEGIN SELECT RAISE(ABORT, 'hook session bindings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS hook_sessions_no_delete BEFORE DELETE ON hook_sessions
BEGIN SELECT RAISE(ABORT, 'hook session bindings cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS ingest_receipts_no_update BEFORE UPDATE ON ingest_receipts
BEGIN SELECT RAISE(ABORT, 'ingest receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ingest_receipts_no_delete BEFORE DELETE ON ingest_receipts
BEGIN SELECT RAISE(ABORT, 'ingest receipts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS consolidation_receipts_no_update BEFORE UPDATE ON consolidation_receipts
BEGIN SELECT RAISE(ABORT, 'consolidation receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS consolidation_receipts_no_delete BEFORE DELETE ON consolidation_receipts
BEGIN SELECT RAISE(ABORT, 'consolidation receipts cannot be deleted'); END;
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(event_id UNINDEXED, content);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(memory_id UNINDEXED, content, summary);
"""


class MemoryStore:
    """Durable SQLite event store with immutable canonical and derived records."""

    MAX_ACTIVE_BYTES = 3000

    def __init__(self, path: str | Path, *, redactor: SecretRedactor | None = None) -> None:
        self._in_memory = str(path) == ":memory:"
        self.path = Path(":memory:") if self._in_memory else Path(path).resolve()
        if not self._in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or SecretRedactor()
        self._lock = threading.RLock()
        self._verified_data_version: int | None = None
        self._verified_schema_version: int | None = None
        target = ":memory:" if self._in_memory else str(self.path)
        self._conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._configure()
            self._initialize()
        except sqlite3.OperationalError as exc:
            self._conn.close()
            if self._in_memory:
                raise RuntimeError("cannot initialize in-memory SQLite store") from exc
            self._conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            try:
                self._configure_rollback_fallback()
                self._initialize()
            except sqlite3.OperationalError as fallback_exc:
                self._conn.close()
                raise RuntimeError(
                    f"cannot initialize durable SQLite store at {self.path}; "
                    "the filesystem supports neither WAL nor exclusive rollback journaling"
                ) from fallback_exc
        try:
            self.assert_integrity()
        except Exception:
            self._conn.close()
            raise

    def _data_version(self) -> int:
        with self._lock:
            row = self._conn.execute("PRAGMA data_version").fetchone()
        return int(row[0])

    def _schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("PRAGMA schema_version").fetchone()
        return int(row[0])

    def assert_integrity(self) -> None:
        """Verify canonical hashes and fail closed if durable data was altered."""
        for _ in range(3):
            before = (self._data_version(), self._schema_version())
            try:
                valid, error = self.verify_chain()
            except Exception as exc:
                raise StoreIntegrityError(
                    f"canonical store could not be verified: {exc}"
                ) from exc
            after = (self._data_version(), self._schema_version())
            if not valid:
                raise StoreIntegrityError(error or "canonical store failed integrity verification")
            if before == after:
                self._verified_data_version, self._verified_schema_version = after
                return
        raise StoreIntegrityError("canonical store changed while integrity was being verified")

    def ensure_integrity(self) -> None:
        """Cheap read guard; re-verify only after another connection committed."""
        self._guard_read()

    def _guard_read(self) -> None:
        current = (self._data_version(), self._schema_version())
        verified = (self._verified_data_version, self._verified_schema_version)
        if verified != current:
            self.assert_integrity()

    def _configure(self) -> None:
        mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if not self._in_memory and str(mode).casefold() != "wal":
            raise sqlite3.OperationalError(f"WAL mode unavailable (got {mode})")
        self.journal_mode = str(mode).casefold()
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _configure_rollback_fallback(self) -> None:
        self._conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        mode = self._conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).casefold() != "delete":
            raise sqlite3.OperationalError(f"rollback journal unavailable (got {mode})")
        self.journal_mode = "delete-exclusive"
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _initialize(self) -> None:
        with self._lock:
            self._conn.executescript(BASE_SCHEMA)
            try:
                self._conn.executescript(FTS_SCHEMA)
                self.fts_enabled = True
                self._ensure_fts_index()
            except sqlite3.OperationalError:
                self.fts_enabled = False
            self._conn.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', '3') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO branches(id, parent_id, fork_event_seq, created_at) "
                "VALUES('main', NULL, NULL, ?)",
                (_now(),),
            )

    def _ensure_fts_index(self) -> None:
        self._conn.execute(
            "INSERT INTO events_fts(event_id, content) "
            "SELECT e.id, e.content FROM events e "
            "WHERE NOT EXISTS (SELECT 1 FROM events_fts f WHERE f.event_id=e.id)"
        )
        self._conn.execute(
            "INSERT INTO memories_fts(memory_id, content, summary) "
            "SELECT m.id, m.content, m.summary FROM memory_records m "
            "WHERE NOT EXISTS (SELECT 1 FROM memories_fts f WHERE f.memory_id=m.id)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def create_branch(
        self, branch_id: str, *, parent_id: str = "main", fork_event_seq: int | None = None
    ) -> str:
        if not branch_id or branch_id == "main":
            raise ValueError("branch_id must be non-empty and cannot recreate main")
        with self._transaction() as conn:
            parent = conn.execute("SELECT id FROM branches WHERE id=?", (parent_id,)).fetchone()
            if parent is None:
                raise KeyError(f"unknown parent branch: {parent_id}")
            parent_events = self.query_events(branch_id=parent_id)
            parent_tip = parent_events[-1].seq if parent_events else 0
            if fork_event_seq is None:
                fork_event_seq = parent_tip
            if fork_event_seq < 0 or fork_event_seq > parent_tip:
                raise ValueError("fork_event_seq must be within the visible parent history")
            conn.execute(
                "INSERT INTO branches(id, parent_id, fork_event_seq, created_at) VALUES(?,?,?,?)",
                (branch_id, parent_id, fork_event_seq, _now()),
            )
        return branch_id

    def start_session(
        self, agent: str, *, branch_id: str = "main", capabilities: dict[str, Any] | None = None
    ) -> str:
        session_id = f"ses_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM branches WHERE id=?", (branch_id,)).fetchone() is None:
                raise KeyError(f"unknown branch: {branch_id}")
            conn.execute(
                "INSERT INTO sessions(id, branch_id, agent, capabilities_json, started_at) "
                "VALUES(?,?,?,?,?)",
                (session_id, branch_id, agent, _json(capabilities or {}), _now()),
            )
        return session_id

    def _append_event_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        branch_id: str,
        session_id: str | None,
        kind: str,
        role: str | None,
        content: str,
        payload: dict[str, Any],
        files: Sequence[str],
    ) -> Event:
        branch = conn.execute("SELECT 1 FROM branches WHERE id=?", (branch_id,)).fetchone()
        if branch is None:
            raise KeyError(f"unknown branch: {branch_id}")
        if session_id is not None:
            session = conn.execute(
                "SELECT branch_id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"unknown session: {session_id}")
            if session["branch_id"] != branch_id:
                raise ValueError("session and event must belong to the same branch")

        safe_content, content_redactions = self.redactor.redact_text(content)
        safe_payload, payload_redactions = self.redactor.redact_value(payload)
        safe_files_value, file_redactions = self.redactor.redact_value(list(files))
        redactions = content_redactions + payload_redactions + file_redactions
        if redactions:
            counts: dict[str, int] = {}
            for item in redactions:
                counts[item.rule] = counts.get(item.rule, 0) + item.count
            safe_payload = dict(safe_payload)
            safe_payload["_security_redactions"] = counts

        event_id = f"evt_{uuid.uuid4().hex}"
        created_at = _now()
        canonical = _json(
            {
                "id": event_id,
                "branch_id": branch_id,
                "session_id": session_id,
                "kind": str(kind),
                "role": role,
                "content": safe_content,
                "payload": safe_payload,
                "files": safe_files_value,
                "created_at": created_at,
            }
        )
        content_hash = _hash(canonical)
        previous = conn.execute(
            "SELECT chain_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["chain_hash"] if previous else None
        chain_hash = _hash((previous_hash or "") + content_hash)
        cursor = conn.execute(
            "INSERT INTO events(id, branch_id, session_id, kind, role, content, payload_json, "
            "files_json, created_at, previous_hash, content_hash, chain_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                branch_id,
                session_id,
                str(kind),
                role,
                safe_content,
                _json(safe_payload),
                _json(safe_files_value),
                created_at,
                previous_hash,
                content_hash,
                chain_hash,
            ),
        )
        if self.fts_enabled:
            conn.execute(
                "INSERT INTO events_fts(event_id, content) VALUES(?,?)",
                (event_id, safe_content),
            )
        return Event(
            seq=int(cursor.lastrowid),
            id=event_id,
            branch_id=branch_id,
            session_id=session_id,
            kind=str(kind),
            role=role,
            content=safe_content,
            payload=dict(safe_payload),
            files=tuple(safe_files_value),
            created_at=created_at,
            previous_hash=previous_hash,
            content_hash=content_hash,
            chain_hash=chain_hash,
        )

    def append_event(
        self,
        *,
        kind: str,
        content: str,
        branch_id: str = "main",
        session_id: str | None = None,
        role: str | None = None,
        payload: dict[str, Any] | None = None,
        files: Sequence[str] = (),
    ) -> Event:
        """Commit one canonical event and return only after a FULL-sync transaction."""
        with self._transaction() as conn:
            return self._append_event_in_tx(
                conn,
                branch_id=branch_id,
                session_id=session_id,
                kind=kind,
                role=role,
                content=content,
                payload=payload or {},
                files=files,
            )

    def append_events_once(
        self,
        receipt_key: str,
        events: Sequence[dict[str, Any]],
        *,
        branch_id: str = "main",
        session_id: str | None = None,
    ) -> tuple[tuple[Event, ...], bool]:
        """Append one hook delivery atomically; retries return the original events."""
        if not receipt_key:
            raise ValueError("receipt_key must be non-empty")
        if not events:
            raise ValueError("at least one event is required")
        with self._transaction() as conn:
            receipt = conn.execute(
                "SELECT event_ids_json FROM ingest_receipts WHERE receipt_key=?",
                (receipt_key,),
            ).fetchone()
            if receipt is not None:
                event_ids = json.loads(receipt["event_ids_json"])
                rows = [
                    conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                    for event_id in event_ids
                ]
                if any(row is None for row in rows):
                    raise RuntimeError("ingest receipt references a missing canonical event")
                return tuple(self._event_from_row(row) for row in rows), False

            appended: list[Event] = []
            for item in events:
                appended.append(
                    self._append_event_in_tx(
                        conn,
                        branch_id=branch_id,
                        session_id=session_id,
                        kind=str(item["kind"]),
                        role=item.get("role"),
                        content=str(item.get("content", "")),
                        payload=dict(item.get("payload", {})),
                        files=tuple(str(value) for value in item.get("files", ())),
                    )
                )
            conn.execute(
                "INSERT INTO ingest_receipts(receipt_key, event_ids_json, created_at) "
                "VALUES(?,?,?)",
                (receipt_key, _json([event.id for event in appended]), _now()),
            )
            return tuple(appended), True

    def hook_session(
        self,
        agent: str,
        external_session_id: str,
        *,
        branch_id: str = "main",
        capabilities: dict[str, Any] | None = None,
    ) -> str:
        """Resolve an immutable native-session to core-session binding."""
        if not agent or not external_session_id:
            raise ValueError("agent and external_session_id must be non-empty")
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT branch_id, session_id FROM hook_sessions "
                "WHERE agent=? AND external_session_id=?",
                (agent, external_session_id),
            ).fetchone()
            if existing is not None:
                if existing["branch_id"] != branch_id:
                    raise ValueError("native session is already bound to another branch")
                return str(existing["session_id"])
            if conn.execute("SELECT 1 FROM branches WHERE id=?", (branch_id,)).fetchone() is None:
                raise KeyError(f"unknown branch: {branch_id}")
            session_id = f"ses_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO sessions(id, branch_id, agent, capabilities_json, started_at) "
                "VALUES(?,?,?,?,?)",
                (session_id, branch_id, agent, _json(capabilities or {}), _now()),
            )
            conn.execute(
                "INSERT INTO hook_sessions(agent, external_session_id, branch_id, session_id, "
                "created_at) VALUES(?,?,?,?,?)",
                (agent, external_session_id, branch_id, session_id, _now()),
            )
            return session_id

    @integrity_checked
    def consolidation_result(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM consolidation_receipts WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return json.loads(row["result_json"]) if row is not None else None

    def mark_consolidated(self, event_id: str, result: dict[str, Any]) -> None:
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is None:
                raise KeyError(f"unknown event: {event_id}")
            conn.execute(
                "INSERT OR IGNORE INTO consolidation_receipts(event_id, result_json, created_at) "
                "VALUES(?,?,?)",
                (event_id, _json(result), _now()),
            )

    def append_artifact(
        self,
        *,
        name: str,
        data: bytes | str,
        mime_type: str = "text/plain",
        branch_id: str = "main",
        session_id: str | None = None,
        files: Sequence[str] = (),
    ) -> Artifact:
        safe_name, _ = self.redactor.redact_text(name)
        safe_mime, _ = self.redactor.redact_text(mime_type)
        textual = isinstance(data, str) or safe_mime.startswith("text/") or safe_mime.endswith("json")
        if isinstance(data, str):
            decoded = data
        elif textual:
            decoded = data.decode("utf-8")
        else:
            decoded = data.decode("latin-1")
        safe_text, redactions = self.redactor.redact_text(decoded)
        if redactions and not textual:
            raise ValueError("binary artifact appears to contain a secret; refusing durable write")
        safe_data = safe_text.encode("utf-8") if textual else bytes(data)
        artifact_id = f"art_{uuid.uuid4().hex}"
        created_at = _now()
        content_hash = _hash(safe_data)
        with self._transaction() as conn:
            event = self._append_event_in_tx(
                conn,
                branch_id=branch_id,
                session_id=session_id,
                kind="artifact",
                role=None,
                content=f"artifact:{safe_name}",
                payload={
                    "artifact_id": artifact_id,
                    "name": safe_name,
                    "mime_type": safe_mime,
                    "content_hash": content_hash,
                },
                files=files,
            )
            conn.execute(
                "INSERT INTO artifacts(id, event_id, name, mime_type, content_hash, data, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (artifact_id, event.id, safe_name, safe_mime, content_hash, safe_data, created_at),
            )
        return Artifact(
            id=artifact_id,
            event_id=event.id,
            name=safe_name,
            mime_type=safe_mime,
            content_hash=content_hash,
            data=safe_data,
            created_at=created_at,
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            seq=int(row["seq"]),
            id=row["id"],
            branch_id=row["branch_id"],
            session_id=row["session_id"],
            kind=row["kind"],
            role=row["role"],
            content=row["content"],
            payload=json.loads(row["payload_json"]),
            files=tuple(json.loads(row["files_json"])),
            created_at=row["created_at"],
            previous_hash=row["previous_hash"],
            content_hash=row["content_hash"],
            chain_hash=row["chain_hash"],
        )

    @integrity_checked
    def get_event(self, event_id: str) -> Event:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown event: {event_id}")
        return self._event_from_row(row)

    @integrity_checked
    def get_artifact(self, artifact_id: str) -> Artifact:
        with self._lock:
            row = self._conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown artifact: {artifact_id}")
        return Artifact(
            id=row["id"], event_id=row["event_id"], name=row["name"],
            mime_type=row["mime_type"], content_hash=row["content_hash"],
            data=bytes(row["data"]), created_at=row["created_at"],
        )

    def _lineage_locked(self, branch_id: str) -> list[tuple[str, int | None]]:
        lineage: list[tuple[str, int | None]] = []
        current = branch_id
        cutoff: int | None = None
        visited: set[str] = set()
        while current is not None:
            if current in visited:
                raise RuntimeError("branch lineage cycle detected")
            visited.add(current)
            row = self._conn.execute(
                "SELECT parent_id, fork_event_seq FROM branches WHERE id=?", (current,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown branch: {branch_id}")
            lineage.append((current, cutoff))
            parent_cutoff = row["fork_event_seq"]
            if parent_cutoff is not None:
                cutoff = parent_cutoff if cutoff is None else min(cutoff, parent_cutoff)
            current = row["parent_id"]
        lineage.reverse()
        return lineage

    @integrity_checked
    def branch_lineage(self, branch_id: str = "main") -> tuple[tuple[str, int | None], ...]:
        with self._lock:
            return tuple(self._lineage_locked(branch_id))

    @integrity_checked
    def query_events(
        self,
        *,
        branch_id: str = "main",
        after_seq: int = 0,
        through_seq: int | None = None,
        kinds: Sequence[str] = (),
        since: str | None = None,
        until: str | None = None,
        text: str | None = None,
        file: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        with self._lock:
            rows: list[sqlite3.Row] = []
            for visible_branch, cutoff in self._lineage_locked(branch_id):
                clauses = ["branch_id=?", "seq>?"]
                params: list[Any] = [visible_branch, after_seq]
                effective_cutoff = cutoff
                if through_seq is not None:
                    effective_cutoff = (
                        through_seq if effective_cutoff is None else min(effective_cutoff, through_seq)
                    )
                if effective_cutoff is not None:
                    clauses.append("seq<=?")
                    params.append(effective_cutoff)
                if kinds:
                    clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
                    params.extend(str(kind) for kind in kinds)
                if since:
                    clauses.append("created_at>=?")
                    params.append(since)
                if until:
                    clauses.append("created_at<=?")
                    params.append(until)
                rows.extend(
                    self._conn.execute(
                        "SELECT * FROM events WHERE " + " AND ".join(clauses), params
                    ).fetchall()
                )
        events = [self._event_from_row(row) for row in sorted(rows, key=lambda item: item["seq"])]
        if text:
            needle = text.casefold()
            events = [
                event for event in events
                if needle in event.content.casefold() or needle in _json(event.payload).casefold()
            ]
        if file:
            events = [event for event in events if file in event.files]
        return events[-limit:] if limit is not None else events

    def _visibility_sql_locked(
        self, alias: str, branch_id: str, cursor_column: str
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for visible_branch, cutoff in self._lineage_locked(branch_id):
            clause = f"({alias}.branch_id=?"
            params.append(visible_branch)
            if cutoff is not None:
                clause += f" AND {alias}.{cursor_column}<=?"
                params.append(cutoff)
            clauses.append(clause + ")")
        return "(" + " OR ".join(clauses) + ")", params

    @integrity_checked
    def search_events_fts(
        self,
        query: str,
        *,
        branch_id: str = "main",
        since: str | None = None,
        until: str | None = None,
        file: str | None = None,
        limit: int = 50,
    ) -> list[tuple[Event, float]]:
        match = _fts_match_query(query)
        if not self.fts_enabled or not match or limit < 1:
            return []
        with self._lock:
            visibility, visibility_params = self._visibility_sql_locked(
                "e", branch_id, "seq"
            )
            clauses = ["events_fts MATCH ?", visibility]
            params: list[Any] = [match, *visibility_params]
            if since:
                clauses.append("e.created_at>=?")
                params.append(since)
            if until:
                clauses.append("e.created_at<=?")
                params.append(until)
            params.append(max(limit * 20, 200))
            rows = self._conn.execute(
                "SELECT e.*, bm25(events_fts) AS fts_rank FROM events_fts "
                "JOIN events e ON e.id=events_fts.event_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY fts_rank ASC, e.seq DESC LIMIT ?",
                params,
            ).fetchall()
        result: list[tuple[Event, float]] = []
        for row in rows:
            event = self._event_from_row(row)
            if file and file not in event.files:
                continue
            result.append((event, float(row["fts_rank"])))
            if len(result) >= limit:
                break
        return result

    @integrity_checked
    def search_memories_fts(
        self,
        query: str,
        *,
        branch_id: str = "main",
        memory_types: Sequence[str] = (),
        since: str | None = None,
        until: str | None = None,
        file: str | None = None,
        limit: int = 50,
    ) -> list[tuple[MemoryRecord, float]]:
        match = _fts_match_query(query)
        if not self.fts_enabled or not match or limit < 1:
            return []
        with self._lock:
            visibility, visibility_params = self._visibility_sql_locked(
                "m", branch_id, "cursor_seq"
            )
            clauses = ["memories_fts MATCH ?", visibility]
            params: list[Any] = [match, *visibility_params]
            if memory_types:
                clauses.append("m.memory_type IN (%s)" % ",".join("?" for _ in memory_types))
                params.extend(str(item) for item in memory_types)
            if since:
                clauses.append("m.created_at>=?")
                params.append(since)
            if until:
                clauses.append("m.created_at<=?")
                params.append(until)
            params.append(max(limit * 20, 200))
            rows = self._conn.execute(
                "SELECT m.*, bm25(memories_fts) AS fts_rank FROM memories_fts "
                "JOIN memory_records m ON m.id=memories_fts.memory_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY fts_rank ASC, m.created_at DESC LIMIT ?",
                params,
            ).fetchall()
            superseded: set[str] = set()
            for visible_branch, cutoff in self._lineage_locked(branch_id):
                supersede_clauses = ["branch_id=?", "supersedes_id IS NOT NULL"]
                supersede_params: list[Any] = [visible_branch]
                if cutoff is not None:
                    supersede_clauses.append("cursor_seq<=?")
                    supersede_params.append(cutoff)
                superseded.update(
                    row["supersedes_id"]
                    for row in self._conn.execute(
                        "SELECT supersedes_id FROM memory_records WHERE "
                        + " AND ".join(supersede_clauses),
                        supersede_params,
                    ).fetchall()
                )
        result: list[tuple[MemoryRecord, float]] = []
        for row in rows:
            if row["id"] in superseded:
                continue
            record = self._memory_from_row(row)
            if file and file not in record.files:
                continue
            result.append((record, float(row["fts_rank"])))
            if len(result) >= limit:
                break
        return result

    def _assert_source_events(
        self,
        conn: sqlite3.Connection,
        source_event_ids: Sequence[str],
        *,
        branch_id: str,
    ) -> None:
        unique = tuple(dict.fromkeys(source_event_ids))
        if not unique:
            raise ValueError("derived records require at least one source event")
        placeholders = ",".join("?" for _ in unique)
        rows = conn.execute(
            f"SELECT id, branch_id, seq FROM events WHERE id IN ({placeholders})", unique
        ).fetchall()
        if len(rows) != len(unique):
            raise ValueError("one or more provenance event IDs do not exist")
        lineage = dict(self._lineage_locked(branch_id))
        invisible = [
            row["id"]
            for row in rows
            if row["branch_id"] not in lineage
            or (
                lineage[row["branch_id"]] is not None
                and int(row["seq"]) > int(lineage[row["branch_id"]])
            )
        ]
        if invisible:
            raise ValueError(
                "provenance event IDs are outside the target branch lineage: "
                + ", ".join(sorted(invisible))
            )

    @staticmethod
    def _block_from_row(row: sqlite3.Row) -> ActiveBlock:
        return ActiveBlock(
            id=row["id"], branch_id=row["branch_id"], name=row["name"],
            content=row["content"], version=int(row["version"]),
            source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
            supersedes_id=row["supersedes_id"], created_at=row["created_at"],
        )

    @integrity_checked
    def get_active_blocks(self, *, branch_id: str = "main") -> dict[str, ActiveBlock]:
        with self._lock:
            rows: list[sqlite3.Row] = []
            visible_branches = self._lineage_locked(branch_id)
            for visible_branch, cutoff in visible_branches:
                clause = "branch_id=?" + (" AND cursor_seq<=?" if cutoff is not None else "")
                params: tuple[Any, ...] = (
                    (visible_branch, cutoff) if cutoff is not None else (visible_branch,)
                )
                rows.extend(
                    self._conn.execute(
                        "SELECT * FROM block_versions WHERE " + clause + " ORDER BY cursor_seq",
                        params,
                    ).fetchall()
                )
        superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
        latest: dict[str, ActiveBlock] = {}
        for row in rows:
            if row["id"] not in superseded:
                block = self._block_from_row(row)
                current = latest.get(block.name)
                if current is None or block.created_at > current.created_at:
                    latest[block.name] = block
        return latest

    def set_active_block(
        self,
        name: str,
        content: str,
        *,
        branch_id: str = "main",
        session_id: str | None = None,
        source_event_ids: Sequence[str] = (),
    ) -> ActiveBlock:
        safe_candidate, _ = self.redactor.redact_text(content)
        current_blocks = self.get_active_blocks(branch_id=branch_id)
        active_bytes = sum(
            len((safe_candidate if block_name == str(name) else block.content).encode("utf-8"))
            for block_name, block in current_blocks.items()
        )
        if str(name) not in current_blocks:
            active_bytes += len(safe_candidate.encode("utf-8"))
        if active_bytes > self.MAX_ACTIVE_BYTES:
            raise ValueError(
                f"protected active memory exceeds {self.MAX_ACTIVE_BYTES} UTF-8 bytes; "
                "store detail as sourced archival memory and keep the active block concise"
            )
        with self._transaction() as conn:
            previous = current_blocks.get(str(name))
            update_event = self._append_event_in_tx(
                conn,
                branch_id=branch_id,
                session_id=session_id,
                kind="memory_block",
                role="system",
                content=content,
                payload={"block": str(name), "supersedes": previous.id if previous else None},
                files=(),
            )
            provenance = tuple(dict.fromkeys((*source_event_ids, update_event.id)))
            self._assert_source_events(conn, provenance, branch_id=branch_id)
            block = ActiveBlock(
                id=f"blk_{uuid.uuid4().hex}",
                branch_id=branch_id,
                name=str(name),
                content=update_event.content,
                version=(previous.version + 1 if previous else 1),
                source_event_ids=provenance,
                supersedes_id=previous.id if previous else None,
                created_at=_now(),
            )
            conn.execute(
                "INSERT INTO block_versions(id, branch_id, name, content, version, "
                "source_event_ids_json, supersedes_id, cursor_seq, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    block.id, block.branch_id, block.name, block.content, block.version,
                    _json(block.source_event_ids), block.supersedes_id, update_event.seq,
                    block.created_at,
                ),
            )
        return block

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"], branch_id=row["branch_id"], memory_type=row["memory_type"],
            content=row["content"], summary=row["summary"],
            files=tuple(json.loads(row["files_json"])), risk=float(row["risk"]),
            retrieval_cost=float(row["retrieval_cost"]), version=int(row["version"]),
            source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
            supersedes_id=row["supersedes_id"], created_at=row["created_at"],
        )

    def derive_memory(
        self,
        *,
        memory_type: str,
        content: str,
        source_event_ids: Sequence[str],
        summary: str = "",
        files: Sequence[str] = (),
        branch_id: str = "main",
        risk: float = 0.0,
        retrieval_cost: float = 1.0,
        supersedes_id: str | None = None,
    ) -> MemoryRecord:
        if not 0.0 <= risk <= 1.0:
            raise ValueError("risk must be between 0 and 1")
        if retrieval_cost < 0:
            raise ValueError("retrieval_cost cannot be negative")
        safe_content, _ = self.redactor.redact_text(content)
        safe_summary, _ = self.redactor.redact_text(summary or content[:240])
        safe_files, _ = self.redactor.redact_value(list(files))
        source_event_ids = tuple(dict.fromkeys(source_event_ids))

        with self._transaction() as conn:
            self._assert_source_events(conn, source_event_ids, branch_id=branch_id)
            record_id = f"mem_{uuid.uuid4().hex}"
            version = 1
            if supersedes_id:
                previous = conn.execute(
                    "SELECT branch_id, version FROM memory_records WHERE id=?", (supersedes_id,)
                ).fetchone()
                if previous is None:
                    raise KeyError(f"unknown superseded memory: {supersedes_id}")
                visible_ids = {
                    item.id for item in self.list_memories(
                        branch_id=branch_id, include_superseded=True
                    )
                }
                if supersedes_id not in visible_ids:
                    raise ValueError("cannot supersede a memory outside the branch lineage")
                version = int(previous["version"]) + 1
            derivation = self._append_event_in_tx(
                conn,
                branch_id=branch_id,
                session_id=None,
                kind="state",
                role=None,
                content=f"derived {memory_type}: {safe_summary}",
                payload={
                    "operation": "derive_memory",
                    "memory_id": record_id,
                    "memory_type": str(memory_type),
                    "content": safe_content,
                    "summary": safe_summary,
                    "source_event_ids": source_event_ids,
                    "files": safe_files,
                    "risk": risk,
                    "retrieval_cost": retrieval_cost,
                    "supersedes_id": supersedes_id,
                },
                files=safe_files,
            )
            record = MemoryRecord(
                id=record_id,
                branch_id=branch_id,
                memory_type=str(memory_type),
                content=safe_content,
                summary=safe_summary,
                files=tuple(safe_files),
                risk=risk,
                retrieval_cost=retrieval_cost,
                version=version,
                source_event_ids=source_event_ids,
                supersedes_id=supersedes_id,
                created_at=_now(),
            )
            conn.execute(
                "INSERT INTO memory_records(id, branch_id, memory_type, content, summary, "
                "files_json, risk, retrieval_cost, version, source_event_ids_json, "
                "supersedes_id, cursor_seq, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id, record.branch_id, record.memory_type, record.content,
                    record.summary, _json(record.files), record.risk, record.retrieval_cost,
                    record.version, _json(record.source_event_ids), record.supersedes_id,
                    derivation.seq, record.created_at,
                ),
            )
            if self.fts_enabled:
                conn.execute(
                    "INSERT INTO memories_fts(memory_id, content, summary) VALUES(?,?,?)",
                    (record.id, record.content, record.summary),
                )
        return record

    @integrity_checked
    def get_memory(self, memory_id: str) -> MemoryRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_records WHERE id=?", (memory_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown memory: {memory_id}")
        return self._memory_from_row(row)

    @integrity_checked
    def list_memories(
        self,
        *,
        branch_id: str = "main",
        memory_types: Sequence[str] = (),
        since: str | None = None,
        until: str | None = None,
        file: str | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryRecord]:
        with self._lock:
            rows: list[sqlite3.Row] = []
            for visible_branch, cutoff in self._lineage_locked(branch_id):
                clauses = ["branch_id=?"]
                params: list[Any] = [visible_branch]
                if cutoff is not None:
                    clauses.append("cursor_seq<=?")
                    params.append(cutoff)
                if memory_types:
                    clauses.append("memory_type IN (%s)" % ",".join("?" for _ in memory_types))
                    params.extend(str(item) for item in memory_types)
                if since:
                    clauses.append("created_at>=?")
                    params.append(since)
                if until:
                    clauses.append("created_at<=?")
                    params.append(until)
                rows.extend(
                    self._conn.execute(
                        "SELECT * FROM memory_records WHERE " + " AND ".join(clauses), params
                    ).fetchall()
                )
        if not include_superseded:
            superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
            rows = [row for row in rows if row["id"] not in superseded]
        records = [self._memory_from_row(row) for row in sorted(rows, key=lambda r: r["created_at"])]
        if file:
            records = [record for record in records if file in record.files]
        return records

    @integrity_checked
    def provenance(self, memory_id: str) -> list[Event]:
        record = self.get_memory(memory_id)
        events = [self.get_event(event_id) for event_id in record.source_event_ids]
        lineage = dict(self.branch_lineage(record.branch_id))
        if any(
            event.branch_id not in lineage
            or (lineage[event.branch_id] is not None and event.seq > int(lineage[event.branch_id]))
            for event in events
        ):
            raise RuntimeError(f"memory {memory_id} contains branch-invisible provenance")
        return events

    @staticmethod
    def _snapshot_delta(parent: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        operations: list[dict[str, Any]] = []

        def walk(path: list[str], before: Any, after: Any) -> None:
            if isinstance(before, dict) and isinstance(after, dict):
                for key in sorted(set(before) - set(after)):
                    operations.append({"op": "remove", "path": [*path, str(key)]})
                for key in sorted(set(after) - set(before)):
                    operations.append(
                        {"op": "set", "path": [*path, str(key)], "value": after[key]}
                    )
                for key in sorted(set(before) & set(after)):
                    walk([*path, str(key)], before[key], after[key])
            elif before != after:
                operations.append({"op": "set", "path": path, "value": after})

        walk([], parent, current)
        return {"format": "json-patch-v2", "operations": operations}

    @staticmethod
    def _apply_snapshot_delta(parent: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
        if delta.get("format") == "incremental-v1":
            state = dict(parent)
            for key in delta.get("remove", []):
                state.pop(key, None)
            state.update(delta.get("set", {}))
            return state
        if delta.get("format") != "json-patch-v2":
            return dict(delta)
        state = copy.deepcopy(parent)
        for operation in delta.get("operations", []):
            path = list(operation.get("path", []))
            if not path:
                if operation.get("op") == "set":
                    state = copy.deepcopy(operation.get("value", {}))
                elif operation.get("op") == "remove":
                    state = {}
                continue
            target: dict[str, Any] = state
            for key in path[:-1]:
                child = target.get(key)
                if not isinstance(child, dict):
                    child = {}
                    target[key] = child
                target = child
            if operation.get("op") == "remove":
                target.pop(path[-1], None)
            elif operation.get("op") == "set":
                target[path[-1]] = copy.deepcopy(operation.get("value"))
        return state

    def _materialize_snapshot_locked(
        self, snapshot_id: str, *, seen: set[str] | None = None
    ) -> tuple[sqlite3.Row, dict[str, Any]]:
        seen = seen or set()
        if snapshot_id in seen:
            raise RuntimeError("snapshot lineage cycle detected")
        seen.add(snapshot_id)
        row = self._conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown snapshot: {snapshot_id}")
        parent_state: dict[str, Any] = {}
        if row["parent_snapshot_id"]:
            _, parent_state = self._materialize_snapshot_locked(
                row["parent_snapshot_id"], seen=seen
            )
        return row, self._apply_snapshot_delta(parent_state, json.loads(row["state_json"]))

    def create_snapshot(
        self,
        *,
        state: dict[str, Any],
        project: dict[str, Any],
        branch_id: str = "main",
        parent_snapshot_id: str | None = None,
    ) -> Snapshot:
        snapshot_id = f"snp_{uuid.uuid4().hex}"
        created_at = _now()
        with self._transaction() as conn:
            lineage = self._lineage_locked(branch_id)
            if parent_snapshot_id is None:
                row = self._latest_visible_snapshot_row_locked(branch_id)
                parent_snapshot_id = row["id"] if row else None
            parent_state: dict[str, Any] = {}
            if parent_snapshot_id:
                parent_row, parent_state = self._materialize_snapshot_locked(parent_snapshot_id)
                lineage_limits = dict(lineage)
                if parent_row["branch_id"] not in lineage_limits:
                    raise ValueError("parent snapshot is outside the branch lineage")
                cutoff = lineage_limits[parent_row["branch_id"]]
                if cutoff is not None and int(parent_row["cursor_seq"]) > cutoff:
                    raise ValueError("parent snapshot was created beyond the branch fork")
            visible_events = self.query_events(branch_id=branch_id)
            cursor_seq = visible_events[-1].seq if visible_events else 0
            delta = self._snapshot_delta(parent_state, state)
            conn.execute(
                "INSERT INTO snapshots(id, branch_id, parent_snapshot_id, cursor_seq, "
                "state_json, project_json, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    snapshot_id, branch_id, parent_snapshot_id, cursor_seq,
                    _json(delta), _json(project), created_at,
                ),
            )
        return Snapshot(
            id=snapshot_id,
            branch_id=branch_id,
            parent_snapshot_id=parent_snapshot_id,
            cursor_seq=cursor_seq,
            state=state,
            project=project,
            created_at=created_at,
        )

    @integrity_checked
    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        with self._lock:
            row, state = self._materialize_snapshot_locked(snapshot_id)
        return Snapshot(
            id=row["id"], branch_id=row["branch_id"],
            parent_snapshot_id=row["parent_snapshot_id"], cursor_seq=int(row["cursor_seq"]),
            state=state, project=json.loads(row["project_json"]), created_at=row["created_at"],
        )

    def _latest_visible_snapshot_row_locked(self, branch_id: str) -> sqlite3.Row | None:
        candidates: list[sqlite3.Row] = []
        for visible_branch, cutoff in self._lineage_locked(branch_id):
            if cutoff is None:
                rows = self._conn.execute(
                    "SELECT * FROM snapshots WHERE branch_id=?", (visible_branch,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM snapshots WHERE branch_id=? AND cursor_seq<=?",
                    (visible_branch, cutoff),
                ).fetchall()
            candidates.extend(rows)
        if not candidates:
            return None
        return max(candidates, key=lambda row: (int(row["cursor_seq"]), row["created_at"]))

    @integrity_checked
    def latest_snapshot(self, *, branch_id: str = "main") -> Snapshot | None:
        with self._lock:
            row = self._latest_visible_snapshot_row_locked(branch_id)
        return self.get_snapshot(row["id"]) if row else None

    @integrity_checked
    def snapshot_tail(
        self, snapshot_id: str, *, target_branch_id: str | None = None
    ) -> list[Event]:
        snapshot = self.get_snapshot(snapshot_id)
        target_branch_id = target_branch_id or snapshot.branch_id
        lineage = dict(self.branch_lineage(target_branch_id))
        if snapshot.branch_id not in lineage:
            raise ValueError("snapshot is outside the target branch lineage")
        cutoff = lineage[snapshot.branch_id]
        if cutoff is not None and snapshot.cursor_seq > cutoff:
            raise ValueError("snapshot was created beyond the target branch fork")
        return self.query_events(branch_id=target_branch_id, after_seq=snapshot.cursor_seq)

    @staticmethod
    def _tool_view_from_row(row: sqlite3.Row) -> ToolOutputView:
        return ToolOutputView(
            id=row["id"], event_id=row["event_id"], level=row["level"],
            reducer=row["reducer"], reducer_version=row["reducer_version"],
            content=row["content"], source_hash=row["source_hash"],
            content_hash=row["content_hash"], raw_bytes=int(row["raw_bytes"]),
            view_bytes=int(row["view_bytes"]), raw_tokens=int(row["raw_tokens"]),
            view_tokens=int(row["view_tokens"]), latency_ns=int(row["latency_ns"]),
            metadata=json.loads(row["metadata_json"]), created_at=row["created_at"],
        )

    def save_tool_output_view(self, **values: Any) -> ToolOutputView:
        required = {
            "event_id", "level", "reducer", "reducer_version", "content", "source_hash",
            "content_hash", "raw_bytes", "view_bytes", "raw_tokens", "view_tokens",
            "latency_ns", "metadata",
        }
        missing = required - set(values)
        if missing:
            raise ValueError(f"missing tool output view fields: {', '.join(sorted(missing))}")
        if values["level"] not in {"compact", "summary"}:
            raise ValueError("tool output view level must be compact or summary")
        safe_content, _ = self.redactor.redact_text(str(values["content"]))
        safe_metadata, _ = self.redactor.redact_value(dict(values["metadata"]))
        with self._transaction() as conn:
            event = conn.execute(
                "SELECT kind, content_hash FROM events WHERE id=?", (values["event_id"],)
            ).fetchone()
            if event is None:
                raise KeyError(f"unknown event: {values['event_id']}")
            if event["kind"] != "tool_output":
                raise ValueError("derived output views require a tool_output source event")
            if event["content_hash"] != values["source_hash"]:
                raise ValueError("tool output view source hash does not match canonical event")
            view_id = f"view_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT OR IGNORE INTO tool_output_views("
                "id,event_id,level,reducer,reducer_version,content,source_hash,content_hash,"
                "raw_bytes,view_bytes,raw_tokens,view_tokens,latency_ns,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    view_id, values["event_id"], values["level"], values["reducer"],
                    values["reducer_version"], safe_content, values["source_hash"],
                    values["content_hash"], int(values["raw_bytes"]), int(values["view_bytes"]),
                    int(values["raw_tokens"]), int(values["view_tokens"]),
                    int(values["latency_ns"]), _json(safe_metadata), _now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM tool_output_views WHERE event_id=? AND level=? AND reducer=? "
                "AND reducer_version=?",
                (
                    values["event_id"], values["level"], values["reducer"],
                    values["reducer_version"],
                ),
            ).fetchone()
        assert row is not None
        return self._tool_view_from_row(row)

    @integrity_checked
    def get_tool_output_view(
        self, event_id: str, *, level: str = "compact"
    ) -> ToolOutputView | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tool_output_views WHERE event_id=? AND level=? "
                "ORDER BY created_at DESC LIMIT 1", (event_id, level),
            ).fetchone()
        return self._tool_view_from_row(row) if row is not None else None

    @integrity_checked
    def get_tool_output_view_by_id(self, view_id: str) -> ToolOutputView:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tool_output_views WHERE id=?", (view_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown tool output view: {view_id}")
        return self._tool_view_from_row(row)

    @integrity_checked
    def list_tool_output_views(self, event_id: str) -> tuple[ToolOutputView, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tool_output_views WHERE event_id=? ORDER BY view_tokens DESC",
                (event_id,),
            ).fetchall()
        return tuple(self._tool_view_from_row(row) for row in rows)

    @staticmethod
    def _usage_from_row(row: sqlite3.Row) -> UsageSample:
        return UsageSample(
            id=row["id"], branch_id=row["branch_id"], session_id=row["session_id"],
            event_id=row["event_id"], source=row["source"], operation=row["operation"],
            input_tokens=int(row["input_tokens"]), output_tokens=int(row["output_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_write_tokens=int(row["cache_write_tokens"]),
            context_tokens=int(row["context_tokens"]), estimated=bool(row["estimated"]),
            cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
            latency_ms=float(row["latency_ms"]) if row["latency_ms"] is not None else None,
            raw_bytes=int(row["raw_bytes"]), emitted_bytes=int(row["emitted_bytes"]),
            metadata=json.loads(row["metadata_json"]), created_at=row["created_at"],
        )

    def record_usage(
        self,
        *,
        branch_id: str = "main",
        session_id: str | None = None,
        event_id: str | None = None,
        source: str,
        operation: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        context_tokens: int = 0,
        estimated: bool = False,
        cost_usd: float | None = None,
        latency_ms: float | None = None,
        raw_bytes: int = 0,
        emitted_bytes: int = 0,
        metadata: dict[str, Any] | None = None,
        receipt_key: str | None = None,
    ) -> UsageSample:
        numeric = (
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            context_tokens, raw_bytes, emitted_bytes,
        )
        if any(int(value) < 0 for value in numeric):
            raise ValueError("usage counters cannot be negative")
        safe_metadata, _ = self.redactor.redact_value(metadata or {})
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM branches WHERE id=?", (branch_id,)).fetchone() is None:
                raise KeyError(f"unknown branch: {branch_id}")
            if receipt_key:
                existing = conn.execute(
                    "SELECT * FROM usage_samples WHERE receipt_key=?", (receipt_key,)
                ).fetchone()
                if existing is not None:
                    return self._usage_from_row(existing)
            sample_id = f"usage_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO usage_samples("
                "id,receipt_key,branch_id,session_id,event_id,source,operation,input_tokens,"
                "output_tokens,cache_read_tokens,cache_write_tokens,context_tokens,estimated,"
                "cost_usd,latency_ms,raw_bytes,emitted_bytes,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sample_id, receipt_key, branch_id, session_id, event_id, source, operation,
                    int(input_tokens), int(output_tokens), int(cache_read_tokens),
                    int(cache_write_tokens), int(context_tokens), int(bool(estimated)), cost_usd,
                    latency_ms, int(raw_bytes), int(emitted_bytes), _json(safe_metadata), _now(),
                ),
            )
            row = conn.execute("SELECT * FROM usage_samples WHERE id=?", (sample_id,)).fetchone()
        assert row is not None
        return self._usage_from_row(row)

    @integrity_checked
    def usage_report(
        self, *, branch_id: str = "main", session_id: str | None = None
    ) -> dict[str, Any]:
        clauses = ["branch_id=?"]
        params: list[Any] = [branch_id]
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM usage_samples WHERE {where} ORDER BY created_at", params
            ).fetchall()
        samples = [self._usage_from_row(row) for row in rows]
        totals = {
            "input_tokens": sum(item.input_tokens for item in samples),
            "output_tokens": sum(item.output_tokens for item in samples),
            "cache_read_tokens": sum(item.cache_read_tokens for item in samples),
            "cache_write_tokens": sum(item.cache_write_tokens for item in samples),
            "raw_bytes": sum(item.raw_bytes for item in samples),
            "emitted_bytes": sum(item.emitted_bytes for item in samples),
            "cost_usd": sum(item.cost_usd or 0.0 for item in samples),
            "sample_count": len(samples),
        }
        reductions = [item for item in samples if item.operation == "tool_output_reduce"]
        raw_tokens = sum(item.input_tokens for item in reductions)
        emitted_tokens = sum(item.output_tokens for item in reductions)
        latencies = sorted(item.latency_ms or 0.0 for item in reductions)
        totals["hook_context_cumulative_tokens"] = self.hook_context_total(
            branch_id=branch_id, session_id=session_id
        )
        totals["tool_output_raw_tokens"] = raw_tokens
        totals["tool_output_emitted_tokens"] = emitted_tokens
        totals["tool_output_token_savings"] = raw_tokens - emitted_tokens
        totals["tool_output_savings_ratio"] = (
            (raw_tokens - emitted_tokens) / raw_tokens if raw_tokens else 0.0
        )
        totals["reducer_latency_ms_p50"] = (
            latencies[len(latencies) // 2] if latencies else 0.0
        )
        totals["reducer_latency_ms_p95"] = (
            latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            if latencies else 0.0
        )
        by_operation: dict[str, dict[str, Any]] = {}
        for item in samples:
            row = by_operation.setdefault(
                item.operation,
                {"samples": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            )
            row["samples"] += 1
            row["input_tokens"] += item.input_tokens
            row["output_tokens"] += item.output_tokens
            row["cost_usd"] += item.cost_usd or 0.0
        return {"branch_id": branch_id, "session_id": session_id, "totals": totals, "by_operation": by_operation}

    @integrity_checked
    def latest_context_usage(
        self, *, branch_id: str = "main", session_id: str | None = None
    ) -> UsageSample | None:
        clauses = ["branch_id=?", "context_tokens>0"]
        params: list[Any] = [branch_id]
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM usage_samples WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
        return self._usage_from_row(row) if row is not None else None

    def record_hook_context_increment(
        self,
        *,
        receipt_key: str,
        branch_id: str,
        session_id: str,
        event_id: str | None,
        event_name: str,
        increment_tokens: int,
    ) -> tuple[int, int, bool]:
        """Append an idempotent increment and return increment, cumulative, created."""
        if increment_tokens < 0:
            raise ValueError("hook context increment cannot be negative")
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT increment_tokens,cumulative_tokens FROM hook_context_counters "
                "WHERE receipt_key=?",
                (receipt_key,),
            ).fetchone()
            if existing is not None:
                return (
                    int(existing["increment_tokens"]),
                    int(existing["cumulative_tokens"]),
                    False,
                )
            previous = conn.execute(
                "SELECT cumulative_tokens FROM hook_context_counters "
                "WHERE branch_id=? AND session_id=? ORDER BY id DESC LIMIT 1",
                (branch_id, session_id),
            ).fetchone()
            cumulative = (int(previous["cumulative_tokens"]) if previous else 0) + increment_tokens
            conn.execute(
                "INSERT INTO hook_context_counters("
                "receipt_key,branch_id,session_id,event_id,event_name,increment_tokens,"
                "cumulative_tokens,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    receipt_key, branch_id, session_id, event_id, event_name,
                    increment_tokens, cumulative, _now(),
                ),
            )
        return increment_tokens, cumulative, True

    @integrity_checked
    def hook_context_total(
        self, *, branch_id: str = "main", session_id: str | None = None
    ) -> int:
        with self._lock:
            if session_id is not None:
                row = self._conn.execute(
                    "SELECT cumulative_tokens total FROM hook_context_counters "
                    "WHERE branch_id=? AND session_id=? ORDER BY id DESC LIMIT 1",
                    (branch_id, session_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(cumulative_tokens),0) total FROM ("
                    "SELECT cumulative_tokens FROM hook_context_counters WHERE branch_id=? "
                    "AND id IN (SELECT MAX(id) FROM hook_context_counters "
                    "WHERE branch_id=? GROUP BY session_id))",
                    (branch_id, branch_id),
                ).fetchone()
        return int(row["total"]) if row is not None else 0
    @integrity_checked
    def latest_provider_context_usage(
        self, *, branch_id: str = "main", session_id: str | None = None
    ) -> UsageSample | None:
        clauses = [
            "branch_id=?", "operation='model_usage'", "estimated=0", "context_tokens>0"
        ]
        params: list[Any] = [branch_id]
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM usage_samples WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
        return self._usage_from_row(row) if row is not None else None

    @staticmethod
    def _budget_policy_from_row(row: sqlite3.Row) -> BudgetPolicy:
        return BudgetPolicy(
            id=row["id"], branch_id=row["branch_id"], version=int(row["version"]),
            context_window_tokens=int(row["context_window_tokens"]),
            snapshot_ratio=float(row["snapshot_ratio"]), compact_ratio=float(row["compact_ratio"]),
            handoff_ratio=float(row["handoff_ratio"]), hard_limit_ratio=float(row["hard_limit_ratio"]),
            min_action_interval_events=int(row["min_action_interval_events"]),
            created_at=row["created_at"],
        )

    def set_budget_policy(
        self,
        *,
        branch_id: str = "main",
        context_window_tokens: int = 200_000,
        snapshot_ratio: float = 0.45,
        compact_ratio: float = 0.60,
        handoff_ratio: float = 0.75,
        hard_limit_ratio: float = 0.90,
        min_action_interval_events: int = 20,
    ) -> BudgetPolicy:
        if context_window_tokens <= 0 or min_action_interval_events < 0:
            raise ValueError("budget sizes and intervals must be positive")
        if not (0 < snapshot_ratio < compact_ratio < handoff_ratio < hard_limit_ratio <= 1):
            raise ValueError("budget ratios must be strictly increasing and at most 1")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM branches WHERE id=?", (branch_id,)).fetchone() is None:
                raise KeyError(f"unknown branch: {branch_id}")
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM budget_policies WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
            version = int(row["version"]) + 1
            policy_id = f"policy_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO budget_policies(id,branch_id,version,context_window_tokens,"
                "snapshot_ratio,compact_ratio,handoff_ratio,hard_limit_ratio,"
                "min_action_interval_events,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    policy_id, branch_id, version, context_window_tokens, snapshot_ratio,
                    compact_ratio, handoff_ratio, hard_limit_ratio, min_action_interval_events,
                    _now(),
                ),
            )
            result = conn.execute("SELECT * FROM budget_policies WHERE id=?", (policy_id,)).fetchone()
        assert result is not None
        return self._budget_policy_from_row(result)

    @integrity_checked
    def get_budget_policy(self, *, branch_id: str = "main") -> BudgetPolicy:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM budget_policies WHERE branch_id=? ORDER BY version DESC LIMIT 1",
                (branch_id,),
            ).fetchone()
        if row is not None:
            return self._budget_policy_from_row(row)
        return BudgetPolicy(
            id=f"default:{branch_id}", branch_id=branch_id, version=0,
            context_window_tokens=200_000, snapshot_ratio=0.45, compact_ratio=0.60,
            handoff_ratio=0.75, hard_limit_ratio=0.90, min_action_interval_events=20,
            created_at="1970-01-01T00:00:00+00:00",
        )

    def record_governor_action(
        self,
        *,
        receipt_key: str,
        branch_id: str,
        action: str,
        reason: str,
        context_tokens: int,
        threshold_tokens: int,
        session_id: str | None = None,
        source_event_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM governor_actions WHERE receipt_key=?", (receipt_key,)
            ).fetchone() is not None:
                return False
            conn.execute(
                "INSERT INTO governor_actions(id,receipt_key,branch_id,session_id,source_event_id,"
                "action,reason,context_tokens,threshold_tokens,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"gov_{uuid.uuid4().hex}", receipt_key, branch_id, session_id,
                    source_event_id, action, reason, context_tokens, threshold_tokens,
                    _json(payload or {}), _now(),
                ),
            )
        return True

    @integrity_checked
    def governor_action_source(self, receipt_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT source_event_id FROM governor_actions WHERE receipt_key=?",
                (receipt_key,),
            ).fetchone()
        return str(row["source_event_id"]) if row is not None and row["source_event_id"] else None

    @integrity_checked
    def last_governor_action_seq(
        self, branch_id: str, action: str, *, policy_id: str | None = None
    ) -> int | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.seq,g.payload_json FROM governor_actions g "
                "LEFT JOIN events e ON e.id=g.source_event_id "
                "WHERE g.branch_id=? AND g.action=? ORDER BY g.created_at DESC",
                (branch_id, action),
            ).fetchall()
        for row in rows:
            if policy_id is not None:
                payload = json.loads(row["payload_json"])
                if payload.get("policy_id") != policy_id:
                    continue
            return int(row["seq"]) if row["seq"] is not None else None
        return None

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
            version = int(current["version"]) + 1 if current is not None else 1
            task_id = f"task_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO task_versions(id,task_key,branch_id,version,title,status,"
                "parent_task_key,source_event_ids_json,snapshot_id,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id, task_key, branch_id, version, safe_title, status,
                    parent_task_key, _json(list(dict.fromkeys(source_event_ids))), snapshot_id,
                    _json(safe_metadata), _now(),
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
                (session_id, task_key, _now()),
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

    def verify_chain(self) -> tuple[bool, str | None]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        previous: str | None = None
        for row in rows:
            canonical = _json(
                {
                    "id": row["id"], "branch_id": row["branch_id"],
                    "session_id": row["session_id"], "kind": row["kind"],
                    "role": row["role"], "content": row["content"],
                    "payload": json.loads(row["payload_json"]),
                    "files": json.loads(row["files_json"]), "created_at": row["created_at"],
                }
            )
            content_hash = _hash(canonical)
            chain_hash = _hash((previous or "") + content_hash)
            if row["previous_hash"] != previous:
                return False, f"event {row['id']} has an invalid previous hash"
            if row["content_hash"] != content_hash or row["chain_hash"] != chain_hash:
                return False, f"event {row['id']} failed integrity verification"
            previous = chain_hash
        with self._lock:
            artifacts = self._conn.execute("SELECT id, content_hash, data FROM artifacts").fetchall()
        for artifact in artifacts:
            if _hash(bytes(artifact["data"])) != artifact["content_hash"]:
                return False, f"artifact {artifact['id']} failed integrity verification"
        with self._lock:
            views = self._conn.execute(
                "SELECT v.id,v.content,v.content_hash,v.source_hash,e.content_hash event_hash "
                "FROM tool_output_views v JOIN events e ON e.id=v.event_id"
            ).fetchall()
        for view in views:
            if _hash(view["content"]) != view["content_hash"]:
                return False, f"tool output view {view['id']} failed integrity verification"
            if view["source_hash"] != view["event_hash"]:
                return False, f"tool output view {view['id']} has an invalid source hash"
        return True, None

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()

    def database_size(self) -> int:
        if self._in_memory:
            return 0
        total = self.path.stat().st_size if self.path.exists() else 0
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total
