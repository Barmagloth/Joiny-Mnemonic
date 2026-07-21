from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import zlib
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import (
    ActiveBlock, Artifact, BudgetPolicy, Event, ExtractionCandidate, MemoryRecord, MemoryType,
    Snapshot, TaskRecord,
    ToolOutputView, UsageSample,
)
from . import temporal
from .provenance import (
    BOOTSTRAP_TOFU,
    EXTERNAL_UNTRUSTED,
    HOST_LOGICAL_USER,
    origin_evidence_type,
)
from .dataflow_storage import DataflowStorageMixin
from .storage_support import integrity_checked
from .transition_rules import (
    CANDIDATE_RULE,
    FINDING_RULE,
    SETTLEMENT_FLOW,
    SETTLEMENT_RULE,
    WORKSTREAM_RULE,
    validate_transition,
)

from .security import SecretRedactor, redaction_counts


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


# task5.md B4 (Headroom text_signals, Apache-2.0): humanized dates and file
# basenames live in a companion FTS column only, so BM25 matches "June 2026"
# or "июня" without polluting displayed content.
_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_RU_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _date_signals(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        value = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    return (
        f"{_EN_MONTHS[value.month - 1]} {value.day} {value.year} "
        f"{value.day} {_RU_MONTHS_GENITIVE[value.month - 1]} {value.year}"
    )


def _file_signals(files: Sequence[str]) -> str:
    names = []
    for item in files:
        base = str(item).replace("\\", "/").rsplit("/", 1)[-1]
        if base and base not in names:
            names.append(base)
    return " ".join(names)


def _event_signals(created_at: str, files: Sequence[str]) -> str:
    return " ".join(part for part in (_date_signals(created_at), _file_signals(files)) if part)


def _memory_signals(
    valid_from: str | None, valid_to: str | None, files: Sequence[str]
) -> str:
    return " ".join(
        part
        for part in (_date_signals(valid_from), _date_signals(valid_to), _file_signals(files))
        if part
    )


def _fts_match_query(value: str) -> str:
    terms = [term for term in re.findall(r"[\w./:-]+", value, re.UNICODE) if term]
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


class StoreIntegrityError(RuntimeError):
    """Raised when canonical storage fails automatic integrity verification."""


class SchemaCompatibilityError(RuntimeError):
    """Raised before mutation when the database schema is unsupported or malformed."""


class SnapshotIntegrityError(RuntimeError):
    """Raised internally when materialized snapshot state fails hash verification."""

    def __init__(self, snapshot_id: str, expected: str, actual: str) -> None:
        self.snapshot_id = snapshot_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"snapshot {snapshot_id} state hash mismatch")


CURRENT_SCHEMA_VERSION = 10
SETTLEMENT_CONFIG_HASH = "settlement-reconciler-v1"
FIRST_VERSIONED_MIGRATION = 7
# v2: memory records serialize bitemporal valid-time fields (task4.md); the
# canonical state layout changed, so rebuilt hashes are only comparable within
# this version.
SNAPSHOT_REPLAY_CODE_VERSION = "snapshot-materializer-v2"


BASE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    from_version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    code_version TEXT NOT NULL,
    backup_path TEXT
);

CREATE TRIGGER IF NOT EXISTS schema_migrations_no_update
BEFORE UPDATE ON schema_migrations
BEGIN SELECT RAISE(ABORT, 'schema migration history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete
BEFORE DELETE ON schema_migrations
BEGIN SELECT RAISE(ABORT, 'schema migration history cannot be deleted'); END;

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

-- Rebuildable projection (health/watermark, 2026-07-17): last known state
-- of each retrieval channel. Hook processes are short-lived, so channel
-- health must survive process boundaries to be visible in capabilities
-- and resume warnings. An empty search result must be distinguishable
-- from a dead or absent channel. Safe to drop at any time.
CREATE TABLE IF NOT EXISTS retrieval_channel_health (
    channel TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Rebuildable projection (task6 packet-assembly fix): SHA-256 per project
-- file keyed by (size, mtime_ns) so resume-time snapshot staleness checks
-- re-hash only files whose stat changed. Safe to drop at any time.
CREATE TABLE IF NOT EXISTS file_hash_cache (
    root TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY(root, path)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    session_id TEXT REFERENCES sessions(id),
    kind TEXT NOT NULL,
    role TEXT,
    origin_channel TEXT NOT NULL,
    origin_adapter TEXT,
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
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    valid_from TEXT,
    valid_to TEXT,
    valid_from_precision TEXT,
    valid_to_precision TEXT,
    temporal_expression TEXT
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

-- Human-facing, append-only execution ledger. This is a derived observability
-- surface, not canonical memory: entries point at canonical/derived ids and
-- retain the redacted values that crossed each boundary.
CREATE TABLE IF NOT EXISTS dataflow_entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL,
    parent_operation_id TEXT,
    operation_name TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    session_id TEXT,
    source TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    refs_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    error_json TEXT NOT NULL,
    duration_ms REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dataflow_operation_seq
ON dataflow_entries(operation_id, seq);
CREATE INDEX IF NOT EXISTS idx_dataflow_branch_seq
ON dataflow_entries(branch_id, seq);

CREATE TRIGGER IF NOT EXISTS dataflow_entries_no_update
BEFORE UPDATE ON dataflow_entries
BEGIN SELECT RAISE(ABORT, 'dataflow entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS dataflow_entries_no_delete
BEFORE DELETE ON dataflow_entries
BEGIN SELECT RAISE(ABORT, 'dataflow entries cannot be deleted'); END;

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
    state_format TEXT NOT NULL DEFAULT 'json-patch-v2',
    state_blob BLOB,
    state_sha256 TEXT,
    replay_code_version TEXT,
    project_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_prunings (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL UNIQUE REFERENCES snapshots(id),
    state_sha256 TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES events(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_ledger (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    policy_hash TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    activation_event_id TEXT NOT NULL REFERENCES events(id),
    previous_policy_id TEXT REFERENCES policy_ledger(id),
    operation TEXT NOT NULL,
    origin_evidence_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS security_findings (
    id TEXT PRIMARY KEY,
    incident_key TEXT NOT NULL UNIQUE,
    finding_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES events(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding_transitions (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL REFERENCES security_findings(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES events(id),
    actor TEXT NOT NULL,
    origin_evidence_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_version ON policy_ledger(version);
CREATE INDEX IF NOT EXISTS idx_findings_type ON security_findings(finding_type);
CREATE INDEX IF NOT EXISTS idx_finding_transitions ON finding_transitions(finding_id, created_at);
CREATE TABLE IF NOT EXISTS extractor_configs (
    config_hash TEXT PRIMARY KEY,
    descriptor_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_wakeups (
    config_hash TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    owner TEXT,
    lease_until REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    extractor_config_hash TEXT NOT NULL REFERENCES extractor_configs(config_hash),
    created_at TEXT NOT NULL,
    UNIQUE(event_id, extractor_config_hash)
);

CREATE TABLE IF NOT EXISTS extraction_attempt_starts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES extraction_runs(id),
    attempt_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    UNIQUE(run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS extraction_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES extraction_runs(id),
    attempt_no INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    error_code TEXT,
    redacted_error TEXT,
    raw_response_ref TEXT,
    UNIQUE(run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS extraction_raw_responses (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES extraction_attempts(id),
    encoding TEXT NOT NULL,
    payload BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES extraction_runs(id),
    attempt_id TEXT NOT NULL REFERENCES extraction_attempts(id),
    memory_type TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    evidence_start INTEGER NOT NULL,
    evidence_end INTEGER NOT NULL,
    evidence_zone TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    valid_from_precision TEXT,
    valid_to_precision TEXT,
    temporal_expression TEXT,
    UNIQUE(run_id, memory_type, normalized_content, evidence_start, evidence_end)
);

CREATE TABLE IF NOT EXISTS extraction_rejections (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES extraction_runs(id),
    attempt_id TEXT NOT NULL REFERENCES extraction_attempts(id),
    candidate_json TEXT NOT NULL,
    error_code TEXT NOT NULL,
    redacted_error TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_transitions (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES extraction_candidates(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES events(id),
    actor TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    origin_evidence_type TEXT NOT NULL,
    replacement_candidate_id TEXT REFERENCES extraction_candidates(id),
    replacement_memory_id TEXT REFERENCES memory_records(id),
    extractor_run_id TEXT REFERENCES extraction_runs(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_memory_links (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES extraction_candidates(id),
    memory_id TEXT NOT NULL REFERENCES memory_records(id),
    relation TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES events(id),
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, memory_id, relation, source_event_id)
);

CREATE VIEW IF NOT EXISTS candidate_current_status AS
SELECT c.id AS candidate_id,
       (SELECT t.to_status FROM candidate_transitions t
        WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1) AS status,
       (SELECT t.created_at FROM candidate_transitions t
        WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1) AS transitioned_at
FROM extraction_candidates c;

DROP VIEW IF EXISTS extraction_run_status;
CREATE VIEW IF NOT EXISTS extraction_run_status AS
SELECT r.id AS run_id,
       r.event_id,
       r.extractor_config_hash,
       CASE
         WHEN EXISTS (
           SELECT 1 FROM extraction_attempts a
           WHERE a.run_id=r.id AND a.outcome='succeeded'
         ) THEN 'done'
         WHEN (
           SELECT a.outcome FROM extraction_attempts a
           WHERE a.run_id=r.id ORDER BY a.attempt_no DESC LIMIT 1
         )='terminal_failure' THEN 'failed'
         WHEN (
           SELECT a.outcome FROM extraction_attempts a
           WHERE a.run_id=r.id ORDER BY a.attempt_no DESC LIMIT 1
         )='retryable_failure' THEN 'retryable'
         WHEN EXISTS (
           SELECT 1 FROM extraction_attempt_starts s
           WHERE s.run_id=r.id AND NOT EXISTS (
             SELECT 1 FROM extraction_attempts a
             WHERE a.run_id=s.run_id AND a.attempt_no=s.attempt_no
           )
         ) AND EXISTS (
           SELECT 1 FROM extraction_wakeups w
           WHERE w.config_hash=r.extractor_config_hash
             AND w.owner IS NOT NULL
             AND w.lease_until > CAST(strftime('%s','now') AS REAL)
         ) THEN 'running'
         WHEN EXISTS (
           SELECT 1 FROM extraction_attempt_starts s
           WHERE s.run_id=r.id AND NOT EXISTS (
             SELECT 1 FROM extraction_attempts a
             WHERE a.run_id=s.run_id AND a.attempt_no=s.attempt_no
           )
         ) THEN 'retryable'
         ELSE 'pending'
       END AS status
FROM extraction_runs r;
CREATE INDEX IF NOT EXISTS idx_extraction_runs_event ON extraction_runs(event_id);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON extraction_attempts(run_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON extraction_candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_transitions_candidate ON candidate_transitions(candidate_id, created_at);
CREATE INDEX IF NOT EXISTS idx_candidate_links_memory ON candidate_memory_links(memory_id);
CREATE INDEX IF NOT EXISTS idx_events_branch_seq ON events(branch_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_branch_type ON memory_records(branch_id, memory_type);
CREATE INDEX IF NOT EXISTS idx_snapshots_branch_cursor ON snapshots(branch_id, cursor_seq);
CREATE INDEX IF NOT EXISTS idx_snapshot_prunings_event ON snapshot_prunings(source_event_id);
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
CREATE TRIGGER IF NOT EXISTS policy_ledger_no_update BEFORE UPDATE ON policy_ledger
BEGIN SELECT RAISE(ABORT, 'policy ledger is immutable'); END;
CREATE TRIGGER IF NOT EXISTS policy_ledger_no_delete BEFORE DELETE ON policy_ledger
BEGIN SELECT RAISE(ABORT, 'policy ledger cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS security_findings_no_update BEFORE UPDATE ON security_findings
BEGIN SELECT RAISE(ABORT, 'security findings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS security_findings_no_delete BEFORE DELETE ON security_findings
BEGIN SELECT RAISE(ABORT, 'security findings cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS finding_transitions_no_update BEFORE UPDATE ON finding_transitions
BEGIN SELECT RAISE(ABORT, 'finding transitions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS finding_transitions_no_delete BEFORE DELETE ON finding_transitions
BEGIN SELECT RAISE(ABORT, 'finding transitions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS extractor_configs_no_update BEFORE UPDATE ON extractor_configs
BEGIN SELECT RAISE(ABORT, 'extractor configurations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS extractor_configs_no_delete BEFORE DELETE ON extractor_configs
BEGIN SELECT RAISE(ABORT, 'extractor configurations cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS extraction_runs_no_update BEFORE UPDATE ON extraction_runs
BEGIN SELECT RAISE(ABORT, 'extraction runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS extraction_runs_no_delete BEFORE DELETE ON extraction_runs
BEGIN SELECT RAISE(ABORT, 'extraction runs cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS extraction_attempt_starts_no_update BEFORE UPDATE ON extraction_attempt_starts
BEGIN SELECT RAISE(ABORT, 'attempt starts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS extraction_attempt_starts_no_delete BEFORE DELETE ON extraction_attempt_starts
BEGIN SELECT RAISE(ABORT, 'attempt starts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS extraction_attempts_no_update BEFORE UPDATE ON extraction_attempts
BEGIN SELECT RAISE(ABORT, 'extraction attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS extraction_attempts_no_delete BEFORE DELETE ON extraction_attempts
BEGIN SELECT RAISE(ABORT, 'extraction attempts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS extraction_raw_no_update BEFORE UPDATE ON extraction_raw_responses
BEGIN SELECT RAISE(ABORT, 'raw extractor responses are immutable'); END;
CREATE TRIGGER IF NOT EXISTS extraction_raw_no_delete BEFORE DELETE ON extraction_raw_responses
BEGIN SELECT RAISE(ABORT, 'raw extractor responses require explicit archival policy'); END;
CREATE TRIGGER IF NOT EXISTS extraction_candidates_no_update BEFORE UPDATE ON extraction_candidates
BEGIN SELECT RAISE(ABORT, 'extraction candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS extraction_candidates_no_delete BEFORE DELETE ON extraction_candidates
BEGIN SELECT RAISE(ABORT, 'extraction candidates cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS extraction_rejections_no_update BEFORE UPDATE ON extraction_rejections
BEGIN SELECT RAISE(ABORT, 'extraction rejections are immutable'); END;
CREATE TRIGGER IF NOT EXISTS extraction_rejections_no_delete BEFORE DELETE ON extraction_rejections
BEGIN SELECT RAISE(ABORT, 'extraction rejections cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS candidate_transitions_no_update BEFORE UPDATE ON candidate_transitions
BEGIN SELECT RAISE(ABORT, 'candidate transitions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS candidate_transitions_no_delete BEFORE DELETE ON candidate_transitions
BEGIN SELECT RAISE(ABORT, 'candidate transitions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS candidate_memory_links_no_update BEFORE UPDATE ON candidate_memory_links
BEGIN SELECT RAISE(ABORT, 'candidate memory links are append-only'); END;
CREATE TRIGGER IF NOT EXISTS candidate_memory_links_no_delete BEFORE DELETE ON candidate_memory_links
BEGIN SELECT RAISE(ABORT, 'candidate memory links cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_prunings_no_update BEFORE UPDATE ON snapshot_prunings
BEGIN SELECT RAISE(ABORT, 'snapshot pruning records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_prunings_no_delete BEFORE DELETE ON snapshot_prunings
BEGIN SELECT RAISE(ABORT, 'snapshot pruning records cannot be deleted'); END;
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
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(event_id UNINDEXED, content, signals);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(memory_id UNINDEXED, content, summary, signals);
"""


class MemoryStore(DataflowStorageMixin):
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
            except BaseException:
                self._conn.close()
                raise
        except BaseException:
            self._conn.close()
            raise
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

    def _stored_storage_schema_version(self) -> int:
        try:
            metadata_exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            if metadata_exists is None:
                return 0
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise SchemaCompatibilityError(
                "database schema metadata is unreadable; no migration was attempted"
            ) from exc
        if row is None:
            return 0
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            raise SchemaCompatibilityError(
                f"invalid database schema version: {row[0]!r}"
            ) from exc
        if version < 0:
            raise SchemaCompatibilityError(
                f"invalid database schema version: {version}"
            )
        return version

    def _has_existing_schema(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        return row is not None

    def _create_schema_backup(self, from_version: int) -> Path:
        if self._in_memory:
            raise RuntimeError("cannot back up an in-memory database")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.path.with_name(
            self.path.name
            + f".pre-migration-v{from_version}-to-v{CURRENT_SCHEMA_VERSION}-{stamp}.bak"
        )
        temporary = backup.with_suffix(backup.suffix + ".tmp")
        destination = sqlite3.connect(temporary)
        try:
            self._conn.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchone()
            if result is None or str(result[0]).casefold() != "ok":
                raise StoreIntegrityError(
                    f"pre-migration backup failed integrity_check: {result}"
                )
            destination.close()
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            temporary.replace(backup)
        except BaseException:
            destination.close()
            temporary.unlink(missing_ok=True)
            raise
        return backup

    def _migrate_to_v7(self) -> None:
        # v7 establishes the immutable migration ledger and future-version gate.
        # BASE_SCHEMA creates the ledger so this step intentionally has no other DDL.
        return None

    def _migrate_to_v9(self) -> None:
        # v9: unified candidate settlement (task6.md 6B). The candidate ledger
        # generalizes beyond extraction via candidate_kind; every legacy row is
        # an 'extraction' candidate by definition. Settlement candidates cite a
        # singleton deterministic extractor config (the reconciler IS a
        # deterministic extractor of closure/block-change candidates), keeping
        # foreign keys honest without a parallel table twin.
        self._ensure_column(
            "extraction_candidates",
            "candidate_kind",
            "TEXT NOT NULL DEFAULT 'extraction'",
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO extractor_configs"
            "(config_hash, descriptor_json, created_at) VALUES(?,?,?)",
            (
                SETTLEMENT_CONFIG_HASH,
                _json({
                    "extractor": "deterministic-settlement",
                    "version": SETTLEMENT_CONFIG_HASH,
                }),
                _now(),
            ),
        )

    def _migrate_to_v10(self) -> None:
        # v10: append-only human-readable dataflow ledger. BASE_SCHEMA creates
        # the table before numbered migrations run, including on legacy stores.
        return None

    def _migrate_to_v8(self) -> None:
        # v8: bitemporal valid-time fields (task4.md). Additive nullable columns
        # only; legacy rows keep NULL = unknown validity. Candidate temporal
        # identity (invariant 8) ships with the Phase B extractor schema, which
        # is the first writer of temporal candidates.
        # No valid-time index: temporal projection is evaluated in Python over
        # the as-of version set (task4.md invariant 3), so SQL never filters
        # by these columns and an index would be dead weight.
        for table in ("memory_records", "extraction_candidates"):
            self._ensure_column(table, "valid_from", "TEXT")
            self._ensure_column(table, "valid_to", "TEXT")
            self._ensure_column(table, "valid_from_precision", "TEXT")
            self._ensure_column(table, "valid_to_precision", "TEXT")
            self._ensure_column(table, "temporal_expression", "TEXT")

    def _apply_schema_migrations(
        self, from_version: int, backup_path: Path | None
    ) -> None:
        current = from_version
        first_target = max(FIRST_VERSIONED_MIGRATION, current + 1)
        for target in range(first_target, CURRENT_SCHEMA_VERSION + 1):
            migration = getattr(self, f"_migrate_to_v{target}", None)
            if migration is None:
                raise SchemaCompatibilityError(
                    f"missing schema migration implementation for version {target}"
                )
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                migration()
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(target),),
                )
                self._conn.execute(
                    "INSERT INTO schema_migrations(version,from_version,applied_at,"
                    "code_version,backup_path) VALUES(?,?,?,?,?)",
                    (
                        target,
                        current,
                        _now(),
                        f"joiny-mnemonic-schema-v{target}",
                        str(backup_path) if backup_path is not None else None,
                    ),
                )
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
            current = target

    def _initialize(self) -> None:
        with self._lock:
            stored_version = self._stored_storage_schema_version()
            if stored_version > CURRENT_SCHEMA_VERSION:
                raise SchemaCompatibilityError(
                    "database schema version "
                    f"{stored_version} is newer than supported version "
                    f"{CURRENT_SCHEMA_VERSION}; upgrade Joiny-Mnemonic before opening it"
                )
            had_schema = self._has_existing_schema()
            backup_path = None
            if (
                stored_version < CURRENT_SCHEMA_VERSION
                and had_schema
                and not self._in_memory
            ):
                backup_path = self._create_schema_backup(stored_version)
            self._conn.executescript(BASE_SCHEMA)
            self._ensure_column("memory_records", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(
                "events", "origin_channel", "TEXT NOT NULL DEFAULT 'legacy_untrusted'"
            )
            self._ensure_column("events", "origin_adapter", "TEXT")
            self._initialize_snapshot_schema()
            try:
                self._conn.executescript(FTS_SCHEMA)
                self._ensure_fts_signals_schema()
                self.fts_enabled = True
                self._ensure_fts_index()
            except sqlite3.OperationalError:
                self.fts_enabled = False
            self._apply_schema_migrations(stored_version, backup_path)
            self._conn.execute(
                "INSERT OR IGNORE INTO branches(id, parent_id, fork_event_seq, created_at) "
                "VALUES('main', NULL, NULL, ?)",
                (_now(),),
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _initialize_snapshot_schema(self) -> None:
        self._ensure_column(
            "snapshots", "state_format", "TEXT NOT NULL DEFAULT 'json-patch-v2'"
        )
        self._ensure_column("snapshots", "state_blob", "BLOB")
        self._ensure_column("snapshots", "state_sha256", "TEXT")
        self._ensure_column("snapshots", "replay_code_version", "TEXT")
        self._conn.execute("DROP TRIGGER IF EXISTS snapshots_no_update")
        try:
            rows = self._conn.execute(
                "SELECT id FROM snapshots WHERE state_sha256 IS NULL "
                "ORDER BY cursor_seq, created_at"
            ).fetchall()
            for row in rows:
                snapshot_row, state = self._materialize_snapshot_locked(
                    str(row["id"]), verify_hash=False
                )
                payload = json.loads(snapshot_row["state_json"])
                legacy_format = (
                    str(payload.get("format", "json-patch-v2"))
                    if isinstance(payload, dict)
                    else "json-patch-v2"
                )
                self._conn.execute(
                    "UPDATE snapshots SET state_format=?,state_sha256=?,"
                    "replay_code_version=? WHERE id=?",
                    (
                        legacy_format,
                        _hash(_json(state)),
                        "legacy-materializer-v2",
                        snapshot_row["id"],
                    ),
                )
        finally:
            self._install_snapshot_triggers()

    def _install_snapshot_triggers(self) -> None:
        self._conn.executescript(
            """
            DROP TRIGGER IF EXISTS snapshots_no_update;
            DROP TRIGGER IF EXISTS snapshots_require_integrity_metadata;
            CREATE TRIGGER snapshots_require_integrity_metadata
            BEFORE INSERT ON snapshots
            WHEN NEW.state_sha256 IS NULL
              OR length(NEW.state_sha256) != 64
              OR NEW.replay_code_version IS NULL
              OR (NEW.state_format='full-zlib-v1' AND NEW.state_blob IS NULL)
            BEGIN SELECT RAISE(ABORT, 'snapshots require hash, replay version and state blob'); END;
            CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON snapshots
            WHEN NOT (
                OLD.state_format='full-zlib-v1'
                AND OLD.state_blob IS NOT NULL
                AND NEW.state_blob IS NULL
                AND NEW.id IS OLD.id
                AND NEW.branch_id IS OLD.branch_id
                AND NEW.parent_snapshot_id IS OLD.parent_snapshot_id
                AND NEW.cursor_seq IS OLD.cursor_seq
                AND NEW.state_json IS OLD.state_json
                AND NEW.state_format IS OLD.state_format
                AND NEW.state_sha256 IS OLD.state_sha256
                AND NEW.replay_code_version IS OLD.replay_code_version
                AND NEW.project_json IS OLD.project_json
                AND NEW.created_at IS OLD.created_at
                AND EXISTS (
                    SELECT 1 FROM snapshot_prunings p
                    WHERE p.snapshot_id=OLD.id
                      AND p.state_sha256=OLD.state_sha256
                )
            )
            BEGIN SELECT RAISE(ABORT, 'snapshots are immutable outside audited blob pruning'); END;
            """
        )
    def _ensure_fts_signals_schema(self) -> None:
        """Rebuild the derived FTS tables when the signals column is missing.

        FTS tables are rebuildable projections; dropping them is legal and
        the reindex below restores content plus the new signals column."""
        try:
            columns = {
                row["name"]
                for row in self._conn.execute(
                    "SELECT name FROM pragma_table_info('events_fts')"
                )
            }
        except sqlite3.DatabaseError:
            return
        if columns and "signals" not in columns:
            self._conn.execute("DROP TABLE IF EXISTS events_fts")
            self._conn.execute("DROP TABLE IF EXISTS memories_fts")
            self._conn.executescript(FTS_SCHEMA)

    def _ensure_fts_index(self) -> None:
        missing_events = self._conn.execute(
            "SELECT e.* FROM events e "
            "WHERE NOT EXISTS (SELECT 1 FROM events_fts f WHERE f.event_id=e.id)"
        ).fetchall()
        for row in missing_events:
            files = tuple(json.loads(row["files_json"]))
            self._conn.execute(
                "INSERT INTO events_fts(event_id, content, signals) VALUES(?,?,?)",
                (row["id"], row["content"], _event_signals(row["created_at"], files)),
            )
        missing_memories = self._conn.execute(
            "SELECT m.* FROM memory_records m "
            "WHERE NOT EXISTS (SELECT 1 FROM memories_fts f WHERE f.memory_id=m.id)"
        ).fetchall()
        for row in missing_memories:
            keys = row.keys()
            self._conn.execute(
                "INSERT INTO memories_fts(memory_id, content, summary, signals) "
                "VALUES(?,?,?,?)",
                (
                    row["id"], row["content"], row["summary"],
                    _memory_signals(
                        row["valid_from"] if "valid_from" in keys else None,
                        row["valid_to"] if "valid_to" in keys else None,
                        tuple(json.loads(row["files_json"])),
                    ),
                ),
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
        origin_channel: str = "internal",
        origin_adapter: str | None = None,
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
            safe_payload = dict(safe_payload)
            safe_payload["_security_redactions"] = redaction_counts(redactions)

        event_id = f"evt_{uuid.uuid4().hex}"
        created_at = _now()
        canonical = _json(
            {
                "id": event_id,
                "branch_id": branch_id,
                "session_id": session_id,
                "kind": str(kind),
                "role": role,
                "origin_channel": origin_channel,
                "origin_adapter": origin_adapter,
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
            "INSERT INTO events(id, branch_id, session_id, kind, role, origin_channel, "
            "origin_adapter, content, payload_json, files_json, created_at, previous_hash, "
            "content_hash, chain_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                branch_id,
                session_id,
                str(kind),
                role,
                origin_channel,
                origin_adapter,
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
                "INSERT INTO events_fts(event_id, content, signals) VALUES(?,?,?)",
                (event_id, safe_content, _event_signals(created_at, tuple(safe_files_value))),
            )
        return Event(
            seq=int(cursor.lastrowid),
            id=event_id,
            branch_id=branch_id,
            session_id=session_id,
            kind=str(kind),
            role=role,
            origin_channel=origin_channel,
            origin_adapter=origin_adapter,
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
                origin_channel="public_api",
                payload=payload or {},
                files=files,
            )

    def append_host_event(
        self,
        *,
        adapter: str,
        kind: str,
        content: str,
        branch_id: str = "main",
        session_id: str | None = None,
        role: str | None = None,
        payload: dict[str, Any] | None = None,
        files: Sequence[str] = (),
    ) -> Event:
        """Commit one event delivered by a configured host hook adapter."""
        if not adapter:
            raise ValueError("adapter must be non-empty")
        with self._transaction() as conn:
            return self._append_event_in_tx(
                conn,
                branch_id=branch_id,
                session_id=session_id,
                kind=kind,
                role=role,
                content=content,
                origin_channel="host_hook",
                origin_adapter=adapter,
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
        """Append an untrusted batch atomically; retries return the original events."""
        return self._append_events_once(
            receipt_key,
            events,
            branch_id=branch_id,
            session_id=session_id,
            origin_channel="public_api",
            origin_adapter=None,
        )

    def append_internal_events_once(
        self,
        receipt_key: str,
        events: Sequence[dict[str, Any]],
        *,
        branch_id: str = "main",
        session_id: str | None = None,
    ) -> tuple[tuple[Event, ...], bool]:
        """Idempotent ingress for deterministic system-derived events
        (reconciler detections and similar); origin is honest: internal."""
        return self._append_events_once(
            receipt_key,
            events,
            branch_id=branch_id,
            session_id=session_id,
            origin_channel="internal",
            origin_adapter=None,
        )

    def append_host_events_once(
        self,
        receipt_key: str,
        events: Sequence[dict[str, Any]],
        *,
        adapter: str,
        branch_id: str = "main",
        session_id: str | None = None,
    ) -> tuple[tuple[Event, ...], bool]:
        """Trusted ingress used only by an installed host hook adapter."""
        if not adapter:
            raise ValueError("adapter must be non-empty")
        return self._append_events_once(
            receipt_key,
            events,
            branch_id=branch_id,
            session_id=session_id,
            origin_channel="host_hook",
            origin_adapter=adapter,
        )

    def _append_events_once(
        self,
        receipt_key: str,
        events: Sequence[dict[str, Any]],
        *,
        branch_id: str,
        session_id: str | None,
        origin_channel: str,
        origin_adapter: str | None,
    ) -> tuple[tuple[Event, ...], bool]:
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
                        origin_channel=origin_channel,
                        origin_adapter=origin_adapter,
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
        safe_name, name_redactions = self.redactor.redact_text(name)
        safe_mime, mime_redactions = self.redactor.redact_text(mime_type)
        textual = isinstance(data, str) or safe_mime.startswith("text/") or safe_mime.endswith("json")
        if isinstance(data, str):
            decoded = data
        elif textual:
            decoded = data.decode("utf-8")
        else:
            decoded = data.decode("latin-1")
        safe_text, data_redactions = self.redactor.redact_text(
            decoded, private_regions=textual
        )
        if data_redactions and not textual:
            raise ValueError("binary artifact appears to contain a secret; refusing durable write")
        safe_data = safe_text.encode("utf-8") if textual else bytes(data)
        artifact_redactions = name_redactions + mime_redactions + data_redactions
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
                    **(
                        {"_security_redactions": redaction_counts(artifact_redactions)}
                        if artifact_redactions
                        else {}
                    ),
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
            origin_channel=row["origin_channel"],
            origin_adapter=row["origin_adapter"],
            content=row["content"],
            payload=json.loads(row["payload_json"]),
            files=tuple(json.loads(row["files_json"])),
            created_at=row["created_at"],
            previous_hash=row["previous_hash"],
            content_hash=row["content_hash"],
            chain_hash=row["chain_hash"],
        )

    @staticmethod
    def _event_origin_evidence(row: sqlite3.Row) -> str:
        return origin_evidence_type(MemoryStore._event_from_row(row))

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
        include_superseded: bool = False,
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
            if not include_superseded:
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
        keys = row.keys()

        def _optional(name: str) -> str | None:
            return row[name] if name in keys else None

        return MemoryRecord(
            id=row["id"], branch_id=row["branch_id"], memory_type=row["memory_type"],
            content=row["content"], summary=row["summary"],
            files=tuple(json.loads(row["files_json"])), risk=float(row["risk"]),
            retrieval_cost=float(row["retrieval_cost"]), version=int(row["version"]),
            source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
            supersedes_id=row["supersedes_id"], created_at=row["created_at"],
            metadata=(
                json.loads(row["metadata_json"])
                if "metadata_json" in keys and row["metadata_json"] else {}
            ),
            valid_from=_optional("valid_from"),
            valid_to=_optional("valid_to"),
            valid_from_precision=_optional("valid_from_precision"),
            valid_to_precision=_optional("valid_to_precision"),
            temporal_expression=_optional("temporal_expression"),
        )

    def _normalize_temporal_input(
        self,
        conn: sqlite3.Connection,
        valid_from: str | None,
        valid_to: str | None,
        source_event_ids: Sequence[str],
    ) -> dict[str, str | None]:
        """Normalize manual valid-time input through the temporal core.

        Relative expressions resolve against the first source event's
        timestamp (task4.md invariant 6); explicit values carry their own
        timezone. All temporal decisions stay inside ``temporal.py``.
        """
        if valid_from is None and valid_to is None:
            return {
                "valid_from": None, "valid_to": None,
                "valid_from_precision": None, "valid_to_precision": None,
            }
        anchor = None
        if source_event_ids:
            row = conn.execute(
                "SELECT created_at FROM events WHERE id=?", (source_event_ids[0],)
            ).fetchone()
            if row is not None:
                anchor = datetime.fromisoformat(row["created_at"])
        start, end = temporal.normalize_interval(
            str(valid_from) if valid_from is not None else None,
            str(valid_to) if valid_to is not None else None,
            anchor=anchor,
        )
        return {
            "valid_from": start.value,
            "valid_to": end.value,
            "valid_from_precision": start.precision if start.value is not None else None,
            "valid_to_precision": end.precision if end.value is not None else None,
        }

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
        metadata: dict[str, Any] | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        temporal_expression: str | None = None,
    ) -> MemoryRecord:
        memory_type = str(memory_type)
        allowed_types = {item.value for item in MemoryType}
        if memory_type not in allowed_types:
            raise ValueError(f"unsupported memory_type: {memory_type}")
        if not 0.0 <= risk <= 1.0:
            raise ValueError("risk must be between 0 and 1")
        if retrieval_cost < 0:
            raise ValueError("retrieval_cost cannot be negative")
        safe_content, _ = self.redactor.redact_text(content)
        safe_summary, _ = self.redactor.redact_text(summary or content[:240])
        safe_files, _ = self.redactor.redact_value(list(files))
        safe_metadata, _ = self.redactor.redact_value(metadata or {})
        safe_expression = None
        if temporal_expression is not None:
            safe_expression, _ = self.redactor.redact_text(str(temporal_expression))
        source_event_ids = tuple(dict.fromkeys(source_event_ids))

        with self._transaction() as conn:
            self._assert_source_events(conn, source_event_ids, branch_id=branch_id)
            temporal_fields = self._normalize_temporal_input(
                conn, valid_from, valid_to, source_event_ids
            )
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
                    "metadata": safe_metadata,
                    "valid_from": temporal_fields["valid_from"],
                    "valid_to": temporal_fields["valid_to"],
                    "valid_from_precision": temporal_fields["valid_from_precision"],
                    "valid_to_precision": temporal_fields["valid_to_precision"],
                    "temporal_expression": safe_expression,
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
                metadata=dict(safe_metadata),
                valid_from=temporal_fields["valid_from"],
                valid_to=temporal_fields["valid_to"],
                valid_from_precision=temporal_fields["valid_from_precision"],
                valid_to_precision=temporal_fields["valid_to_precision"],
                temporal_expression=safe_expression,
            )
            conn.execute(
                "INSERT INTO memory_records(id, branch_id, memory_type, content, summary, "
                "files_json, risk, retrieval_cost, version, source_event_ids_json, "
                "supersedes_id, cursor_seq, created_at, metadata_json, "
                "valid_from, valid_to, valid_from_precision, valid_to_precision, "
                "temporal_expression) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id, record.branch_id, record.memory_type, record.content,
                    record.summary, _json(record.files), record.risk, record.retrieval_cost,
                    record.version, _json(record.source_event_ids), record.supersedes_id,
                    derivation.seq, record.created_at, _json(record.metadata),
                    record.valid_from, record.valid_to, record.valid_from_precision,
                    record.valid_to_precision, record.temporal_expression,
                ),
            )
            if self.fts_enabled:
                conn.execute(
                    "INSERT INTO memories_fts(memory_id, content, summary, signals) "
                    "VALUES(?,?,?,?)",
                    (
                        record.id, record.content, record.summary,
                        _memory_signals(
                            record.valid_from, record.valid_to, record.files
                        ),
                    ),
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
    def completion_evidence_events(
        self, *, branch_id: str = "main", after_seq: int = 0
    ) -> list[Event]:
        """Hot-path support for the reconciler (review finding M5): only
        host-hook tool outputs after the earliest anchor can be completion
        evidence, so the filter belongs in SQL, not in Python over the
        whole materialized lineage."""
        with self._lock:
            rows: list[sqlite3.Row] = []
            for visible_branch, cutoff in self._lineage_locked(branch_id):
                clauses = [
                    "branch_id=?", "seq>?", "kind='tool_output'",
                    "origin_channel='host_hook'",
                ]
                params: list[Any] = [visible_branch, after_seq]
                if cutoff is not None:
                    clauses.append("seq<=?")
                    params.append(cutoff)
                rows.extend(
                    self._conn.execute(
                        "SELECT * FROM events WHERE " + " AND ".join(clauses)
                        + " ORDER BY seq",
                        params,
                    ).fetchall()
                )
        return [self._event_from_row(row) for row in sorted(rows, key=lambda r: r["seq"])]

    @integrity_checked
    def events_by_operation(
        self, operation: str, *, branch_id: str = "main"
    ) -> list[Event]:
        """State events with a given payload operation, filtered in SQL."""
        with self._lock:
            rows: list[sqlite3.Row] = []
            for visible_branch, cutoff in self._lineage_locked(branch_id):
                clauses = [
                    "branch_id=?", "kind='state'",
                    "json_extract(payload_json,'$.operation')=?",
                ]
                params: list[Any] = [visible_branch, operation]
                if cutoff is not None:
                    clauses.append("seq<=?")
                    params.append(cutoff)
                rows.extend(
                    self._conn.execute(
                        "SELECT * FROM events WHERE " + " AND ".join(clauses)
                        + " ORDER BY seq",
                        params,
                    ).fetchall()
                )
        return [self._event_from_row(row) for row in sorted(rows, key=lambda r: r["seq"])]

    @integrity_checked
    def known_at_cutoff_seq(self, known_at: str, *, branch_id: str = "main") -> int:
        """Resolve ``known_at`` to the greatest lineage-visible ``seq`` admitted
        at or before that instant (task4.md invariant 2).

        ``created_at`` is written exclusively by ``_now()`` as UTC ISO 8601, so
        the comparison is exact string order; ties and non-monotonic clocks
        resolve deterministically through the canonical ``seq`` order.
        """
        bound = temporal.normalize_bound(str(known_at))
        if not bound.envelope.singleton:
            raise temporal.TemporalValidationError(
                "known_at_not_instant", "known_at requires an exact timestamp"
            )
        threshold = bound.envelope.lo.astimezone(UTC).isoformat(timespec="microseconds")
        best = 0
        with self._lock:
            for visible_branch, cutoff in self._lineage_locked(branch_id):
                clauses = ["branch_id=?", "created_at<=?"]
                params: list[Any] = [visible_branch, threshold]
                if cutoff is not None:
                    clauses.append("seq<=?")
                    params.append(cutoff)
                row = self._conn.execute(
                    "SELECT MAX(seq) AS cutoff_seq FROM events WHERE "
                    + " AND ".join(clauses),
                    params,
                ).fetchone()
                if row["cutoff_seq"] is not None:
                    best = max(best, int(row["cutoff_seq"]))
        return best

    @integrity_checked
    def memories_as_of(
        self, *, branch_id: str = "main", cutoff_seq: int | None = None
    ) -> list[MemoryRecord]:
        """Lineage-visible memory versions admitted at or before the cutoff.

        Includes superseded versions: this is the projection input required by
        task4.md invariant 3 — derived temporal state must be computed from the
        versions visible at the known-at cutoff, never from full history.
        """
        with self._lock:
            rows: list[sqlite3.Row] = []
            for visible_branch, cutoff in self._lineage_locked(branch_id):
                effective = cutoff
                if cutoff_seq is not None:
                    effective = cutoff_seq if cutoff is None else min(cutoff, cutoff_seq)
                clauses = ["branch_id=?"]
                params: list[Any] = [visible_branch]
                if effective is not None:
                    clauses.append("cursor_seq<=?")
                    params.append(effective)
                rows.extend(
                    self._conn.execute(
                        "SELECT * FROM memory_records WHERE " + " AND ".join(clauses),
                        params,
                    ).fetchall()
                )
        ordered = sorted(rows, key=lambda row: (row["cursor_seq"], row["created_at"]))
        return [self._memory_from_row(row) for row in ordered]

    @integrity_checked
    def memory_lineage_links(self, *, branch_id: str = "main") -> dict[str, str | None]:
        """One-query map ``memory_id -> supersedes_id`` over the full visible
        lineage (no known-at cutoff): the ancestor walk needs post-cutoff links
        to descend from a current version to the one live at the cutoff."""
        links: dict[str, str | None] = {}
        with self._lock:
            for visible_branch, cutoff in self._lineage_locked(branch_id):
                clauses = ["branch_id=?"]
                params: list[Any] = [visible_branch]
                if cutoff is not None:
                    clauses.append("cursor_seq<=?")
                    params.append(cutoff)
                for row in self._conn.execute(
                    "SELECT id, supersedes_id FROM memory_records WHERE "
                    + " AND ".join(clauses),
                    params,
                ).fetchall():
                    links[str(row["id"])] = (
                        str(row["supersedes_id"]) if row["supersedes_id"] else None
                    )
        return links

    @integrity_checked
    def events_created_at(self, event_ids: Sequence[str]) -> dict[str, str]:
        """Batched admission times for observed_at derivation."""
        unique = [item for item in dict.fromkeys(event_ids) if item]
        result: dict[str, str] = {}
        with self._lock:
            for start in range(0, len(unique), 500):
                chunk = unique[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                for row in self._conn.execute(
                    f"SELECT id, created_at FROM events WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall():
                    result[str(row["id"])] = str(row["created_at"])
        return result

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
        self,
        snapshot_id: str,
        *,
        seen: set[str] | None = None,
        verify_hash: bool = True,
    ) -> tuple[sqlite3.Row, dict[str, Any]]:
        seen = seen or set()
        if snapshot_id in seen:
            raise RuntimeError("snapshot lineage cycle detected")
        seen.add(snapshot_id)
        row = self._conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown snapshot: {snapshot_id}")
        state_format = str(row["state_format"] or "json-patch-v2")
        if state_format == "full-zlib-v1":
            if row["state_blob"] is None:
                raise RuntimeError(f"snapshot blob was pruned: {snapshot_id}")
            try:
                decoded = zlib.decompress(bytes(row["state_blob"])).decode("utf-8")
                state = json.loads(decoded)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SnapshotIntegrityError(
                    snapshot_id,
                    str(row["state_sha256"] or "missing"),
                    "unreadable-full-zlib-v1",
                ) from exc
            if not isinstance(state, dict):
                raise SnapshotIntegrityError(
                    snapshot_id,
                    str(row["state_sha256"] or "missing"),
                    _hash(decoded),
                )
            # For the full-blob format the stored sha256 was computed over
            # exactly these bytes at creation: hashing the decompressed
            # text verifies integrity without a canonical re-serialization
            # of the whole state (task6 packet-assembly hot path).
            actual = _hash(decoded)
            expected = row["state_sha256"]
            if verify_hash and expected != actual:
                raise SnapshotIntegrityError(
                    snapshot_id, str(expected or "missing"), actual
                )
            return row, state
        else:
            parent_state: dict[str, Any] = {}
            if row["parent_snapshot_id"]:
                _, parent_state = self._materialize_snapshot_locked(
                    row["parent_snapshot_id"], seen=seen, verify_hash=verify_hash
                )
            try:
                delta = json.loads(row["state_json"])
            except json.JSONDecodeError as exc:
                raise SnapshotIntegrityError(
                    snapshot_id,
                    str(row["state_sha256"] or "missing"),
                    "unreadable-legacy-json",
                ) from exc
            state = self._apply_snapshot_delta(parent_state, delta)
        actual = _hash(_json(state))
        expected = row["state_sha256"]
        if verify_hash and expected != actual:
            raise SnapshotIntegrityError(snapshot_id, str(expected or "missing"), actual)
        return row, state

    def _raise_snapshot_integrity_finding(self, error: SnapshotIntegrityError) -> None:
        details = {
            "snapshot_id": error.snapshot_id,
            "expected_state_sha256": error.expected,
            "actual_state_sha256": error.actual,
        }
        self.record_security_finding(
            "snapshot_state_hash_mismatch",
            incident_key=(
                f"snapshot_state_hash_mismatch:{error.snapshot_id}:"
                f"{error.expected}:{error.actual}"
            ),
            details=details,
        )
        raise StoreIntegrityError(str(error)) from error

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
        state_bytes = _json(state).encode("utf-8")
        materialized_state = json.loads(state_bytes)
        if not isinstance(materialized_state, dict):
            raise ValueError("snapshot state must serialize to a JSON object")
        state_sha256 = _hash(state_bytes)
        state_blob = zlib.compress(state_bytes)
        try:
            with self._transaction() as conn:
                lineage = self._lineage_locked(branch_id)
                if parent_snapshot_id is None:
                    row = self._latest_visible_snapshot_row_locked(branch_id)
                    parent_snapshot_id = row["id"] if row else None
                if parent_snapshot_id:
                    parent_row, _ = self._materialize_snapshot_locked(parent_snapshot_id)
                    lineage_limits = dict(lineage)
                    if parent_row["branch_id"] not in lineage_limits:
                        raise ValueError("parent snapshot is outside the branch lineage")
                    cutoff = lineage_limits[parent_row["branch_id"]]
                    if cutoff is not None and int(parent_row["cursor_seq"]) > cutoff:
                        raise ValueError("parent snapshot was created beyond the branch fork")
                visible_events = self.query_events(branch_id=branch_id)
                cursor_seq = visible_events[-1].seq if visible_events else 0
                conn.execute(
                    "INSERT INTO snapshots(id,branch_id,parent_snapshot_id,cursor_seq,"
                    "state_json,state_format,state_blob,state_sha256,replay_code_version,"
                    "project_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        branch_id,
                        parent_snapshot_id,
                        cursor_seq,
                        _json({"format": "full-zlib-v1"}),
                        "full-zlib-v1",
                        state_blob,
                        state_sha256,
                        SNAPSHOT_REPLAY_CODE_VERSION,
                        _json(project),
                        created_at,
                    ),
                )
        except SnapshotIntegrityError as exc:
            self._raise_snapshot_integrity_finding(exc)
        return Snapshot(
            id=snapshot_id,
            branch_id=branch_id,
            parent_snapshot_id=parent_snapshot_id,
            cursor_seq=cursor_seq,
            state=materialized_state,
            project=project,
            created_at=created_at,
            state_format="full-zlib-v1",
            state_sha256=state_sha256,
            replay_code_version=SNAPSHOT_REPLAY_CODE_VERSION,
            blob_available=True,
        )

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
                    (channel, _json(payload), _now())
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

    @integrity_checked
    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        try:
            with self._lock:
                row, state = self._materialize_snapshot_locked(snapshot_id)
        except SnapshotIntegrityError as exc:
            self._raise_snapshot_integrity_finding(exc)
        return Snapshot(
            id=row["id"],
            branch_id=row["branch_id"],
            parent_snapshot_id=row["parent_snapshot_id"],
            cursor_seq=int(row["cursor_seq"]),
            state=state,
            project=json.loads(row["project_json"]),
            created_at=row["created_at"],
            state_format=str(row["state_format"]),
            state_sha256=row["state_sha256"],
            replay_code_version=row["replay_code_version"],
            blob_available=(
                row["state_blob"] is not None
                or str(row["state_format"]) != "full-zlib-v1"
            ),
        )
    def _latest_visible_snapshot_row_locked(self, branch_id: str) -> sqlite3.Row | None:
        candidates: list[sqlite3.Row] = []
        for visible_branch, cutoff in self._lineage_locked(branch_id):
            if cutoff is None:
                rows = self._conn.execute(
                    "SELECT * FROM snapshots WHERE branch_id=? "
                    "AND (state_format!='full-zlib-v1' OR state_blob IS NOT NULL)",
                    (visible_branch,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM snapshots WHERE branch_id=? AND cursor_seq<=? "
                    "AND (state_format!='full-zlib-v1' OR state_blob IS NOT NULL)",
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
        self, snapshot_id: "str | Snapshot", *, target_branch_id: str | None = None
    ) -> list[Event]:
        # Accepting an already-materialized Snapshot avoids a redundant
        # decompress+verify on the resume hot path (task6 packet-assembly).
        snapshot = (
            snapshot_id
            if isinstance(snapshot_id, Snapshot)
            else self.get_snapshot(snapshot_id)
        )
        target_branch_id = target_branch_id or snapshot.branch_id
        lineage = dict(self.branch_lineage(target_branch_id))
        if snapshot.branch_id not in lineage:
            raise ValueError("snapshot is outside the target branch lineage")
        cutoff = lineage[snapshot.branch_id]
        if cutoff is not None and snapshot.cursor_seq > cutoff:
            raise ValueError("snapshot was created beyond the target branch fork")
        return self.query_events(branch_id=target_branch_id, after_seq=snapshot.cursor_seq)

    @integrity_checked
    def snapshot_replay_tail_size(self, *, branch_id: str = "main") -> int:
        with self._lock:
            row = self._latest_visible_snapshot_row_locked(branch_id)
            after_seq = int(row["cursor_seq"]) if row is not None else 0
        events = self.query_events(branch_id=branch_id, after_seq=after_seq)
        return sum(len(_json(event.to_dict()).encode("utf-8")) for event in events)

    def prune_snapshot_blobs(
        self,
        snapshot_ids: Sequence[str],
        *,
        branch_id: str = "main",
    ) -> dict[str, Any]:
        requested = tuple(dict.fromkeys(str(item) for item in snapshot_ids))
        if not requested:
            raise ValueError("at least one snapshot ID is required")
        with self._lock:
            preflight = {
                str(row["id"]): row
                for row in self._conn.execute(
                    "SELECT * FROM snapshots WHERE id IN (%s)"
                    % ",".join("?" for _ in requested),
                    requested,
                ).fetchall()
            }
        for snapshot_id in requested:
            row = preflight.get(snapshot_id)
            if row is None:
                raise KeyError(f"unknown snapshot: {snapshot_id}")
            if row["state_format"] != "full-zlib-v1":
                raise ValueError(f"legacy snapshot blobs are not prunable: {snapshot_id}")
            if row["state_blob"] is not None:
                self.get_snapshot(snapshot_id)
        with self._transaction() as conn:
            rows = []
            for snapshot_id in requested:
                row = conn.execute(
                    "SELECT * FROM snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown snapshot: {snapshot_id}")
                if row["state_format"] != "full-zlib-v1":
                    raise ValueError(
                        f"legacy snapshot blobs are not prunable: {snapshot_id}"
                    )
                if row["state_blob"] is not None:
                    rows.append(row)
            if not rows:
                return {"event_id": None, "pruned": []}

            protected: dict[str, str] = {}
            usage_rows = conn.execute(
                "SELECT operation,metadata_json FROM usage_samples"
            ).fetchall()
            for usage in usage_rows:
                metadata = json.loads(usage["metadata_json"])
                if not isinstance(metadata, dict):
                    continue
                snapshot_id = metadata.get("snapshot_id")
                if snapshot_id:
                    protected[str(snapshot_id)] = f"usage:{usage['operation']}"
            task_rows = conn.execute(
                "SELECT t.snapshot_id,t.task_key FROM task_versions t "
                "JOIN (SELECT task_key,MAX(version) AS version FROM task_versions "
                "GROUP BY task_key) latest "
                "ON latest.task_key=t.task_key AND latest.version=t.version "
                "WHERE t.status IN ('active','blocked') AND t.snapshot_id IS NOT NULL"
            ).fetchall()
            for task in task_rows:
                protected[str(task["snapshot_id"])] = f"active_task:{task['task_key']}"
            blocked = {
                str(row["id"]): protected[str(row["id"])]
                for row in rows
                if str(row["id"]) in protected
            }
            if blocked:
                rendered = ", ".join(
                    f"{snapshot_id} ({reason})"
                    for snapshot_id, reason in sorted(blocked.items())
                )
                raise ValueError(f"protected snapshots cannot be pruned: {rendered}")

            items = [
                {
                    "snapshot_id": str(row["id"]),
                    "state_sha256": str(row["state_sha256"]),
                }
                for row in rows
            ]
            event = self._append_event_in_tx(
                conn,
                branch_id=branch_id,
                session_id=None,
                kind="state",
                role=None,
                content=f"pruned {len(items)} snapshot blob(s)",
                origin_channel="internal",
                origin_adapter="snapshot_pruner",
                payload={"operation": "snapshots_pruned", "snapshots": items},
                files=(),
            )
            for item in items:
                conn.execute(
                    "INSERT INTO snapshot_prunings(id,snapshot_id,state_sha256,"
                    "source_event_id,created_at) VALUES(?,?,?,?,?)",
                    (
                        f"spr_{uuid.uuid4().hex}",
                        item["snapshot_id"],
                        item["state_sha256"],
                        event.id,
                        _now(),
                    ),
                )
                conn.execute(
                    "UPDATE snapshots SET state_blob=NULL WHERE id=?",
                    (item["snapshot_id"],),
                )
        return {"event_id": event.id, "pruned": items}
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
    def list_usage_samples(
        self,
        *,
        branch_id: str = "main",
        session_id: str | None = None,
        operation: str | None = None,
    ) -> tuple[UsageSample, ...]:
        clauses = ["branch_id=?"]
        params: list[Any] = [branch_id]
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        if operation is not None:
            clauses.append("operation=?")
            params.append(operation)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM usage_samples WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at",
                params,
            ).fetchall()
        return tuple(self._usage_from_row(row) for row in rows)

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
        retrieval_exposures = [
            item for item in samples if item.operation == "retrieval_search"
        ]
        prompt_exposures = [
            item for item in samples if item.operation == "prompt_injection"
        ]
        totals["retrieval_search_count"] = len(retrieval_exposures)
        totals["retrieval_result_count"] = sum(
            len(item.metadata.get("results", ())) for item in retrieval_exposures
        )
        totals["prompt_injection_count"] = len(prompt_exposures)
        totals["prompt_included_event_count"] = sum(
            len(item.metadata.get("included_event_ids", ())) for item in prompt_exposures
        )
        totals["prompt_included_memory_count"] = sum(
            len(item.metadata.get("included_memory_ids", ())) for item in prompt_exposures
        )
        totals["task_correlated_exposure_count"] = sum(
            1
            for item in (*retrieval_exposures, *prompt_exposures)
            if item.metadata.get("task_key")
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

    def initialize_project(
        self,
        *,
        repository_identity: str,
        canonical_path: str,
        code_version: str,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT value FROM metadata WHERE key='project_instance_id'"
            ).fetchone()
            if existing is not None:
                project_instance_id = str(existing["value"])
                incident_key = f"policy_rebootstrapped:{project_instance_id}"
                finding = conn.execute(
                    "SELECT id FROM security_findings WHERE incident_key=?",
                    (incident_key,),
                ).fetchone()
                if finding is None:
                    event = self._append_event_in_tx(
                        conn,
                        branch_id="main",
                        session_id=None,
                        kind="state",
                        role=None,
                        content="security finding: repeated project bootstrap",
                        payload={
                            "operation": "security_finding",
                            "finding_type": "policy_rebootstrapped",
                            "project_instance_id": project_instance_id,
                        },
                        files=(),
                    )
                    finding_id = f"finding_{uuid.uuid4().hex}"
                    conn.execute(
                        "INSERT INTO security_findings"
                        "(id, incident_key, finding_type, details_json, "
                        "source_event_id, created_at) VALUES(?,?,?,?,?,?)",
                        (
                            finding_id, incident_key, "policy_rebootstrapped",
                            _json({"project_instance_id": project_instance_id}),
                            event.id, _now(),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO finding_transitions"
                        "(id, finding_id, from_status, to_status, source_event_id, "
                        "actor, origin_evidence_type, created_at) "
                        "VALUES(?,?,NULL,'active',?,?,?,?)",
                        (
                            f"ftr_{uuid.uuid4().hex}", finding_id, event.id,
                            "bootstrap_guard", "bootstrap_tofu", _now(),
                        ),
                    )
                return {
                    "project_instance_id": project_instance_id,
                    "initialized": False,
                    "finding": "policy_rebootstrapped",
                }

            project_instance_id = f"project_{uuid.uuid4().hex}"
            chain_id = f"chain_{uuid.uuid4().hex}"
            bootstrap = {
                "project_instance_id": project_instance_id,
                "chain_id": chain_id,
                "repository_identity": repository_identity,
                "canonical_path": canonical_path,
                "code_version": code_version,
                "policy": policy,
                "origin_evidence_type": "bootstrap_tofu",
            }
            bootstrap_hash = _hash(_json(bootstrap))
            event = self._append_event_in_tx(
                conn,
                branch_id="main",
                session_id=None,
                kind="state",
                role=None,
                content="policy bootstrapped with trust-on-first-use",
                payload={
                    "operation": "policy_bootstrapped",
                    **bootstrap,
                    "bootstrap_hash": bootstrap_hash,
                },
                files=(),
            )
            for key, value in {
                "project_instance_id": project_instance_id,
                "chain_id": chain_id,
                "repository_identity": repository_identity,
                "canonical_path": canonical_path,
                "bootstrap_hash": bootstrap_hash,
                "bootstrap_event_id": event.id,
            }.items():
                conn.execute(
                    "INSERT INTO metadata(key, value) VALUES(?,?)", (key, value)
                )
            policy_id = f"policy_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO policy_ledger"
                "(id, version, policy_hash, policy_json, activation_event_id, "
                "previous_policy_id, operation, origin_evidence_type, created_at) "
                "VALUES(?,?,?,?,?,NULL,'bootstrapped','bootstrap_tofu',?)",
                (
                    policy_id, 1, _hash(_json(policy)), _json(policy), event.id, _now(),
                ),
            )
        return {
            "project_instance_id": project_instance_id,
            "chain_id": chain_id,
            "bootstrap_hash": bootstrap_hash,
            "policy_id": policy_id,
            "event_id": event.id,
            "initialized": True,
        }

    @integrity_checked
    def project_identity(self) -> dict[str, str] | None:
        keys = (
            "project_instance_id", "chain_id", "repository_identity",
            "canonical_path", "bootstrap_hash", "bootstrap_event_id",
        )
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM metadata WHERE key IN (%s)"
                % ",".join("?" for _ in keys),
                keys,
            ).fetchall()
        values = {row["key"]: row["value"] for row in rows}
        return values if "project_instance_id" in values else None

    @integrity_checked
    def chain_checkpoint(self) -> dict[str, Any]:
        with self._lock:
            head = self._conn.execute(
                "SELECT seq, chain_hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return {
            "head_seq": int(head["seq"]) if head else 0,
            "head_hash": str(head["chain_hash"]) if head else "",
        }

    @integrity_checked
    def chain_hash_at(self, seq: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT chain_hash FROM events WHERE seq=?", (seq,)
            ).fetchone()
        return str(row["chain_hash"]) if row else None

    def record_security_finding(
        self,
        finding_type: str,
        *,
        incident_key: str,
        details: dict[str, Any],
    ) -> str:
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM security_findings WHERE incident_key=?",
                (incident_key,),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            event = self._append_event_in_tx(
                conn,
                branch_id="main",
                session_id=None,
                kind="state",
                role=None,
                content=f"security finding: {finding_type}",
                payload={
                    "operation": "security_finding",
                    "finding_type": finding_type,
                    "incident_key": incident_key,
                    "details": details,
                },
                files=(),
            )
            finding_id = f"finding_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO security_findings"
                "(id, incident_key, finding_type, details_json, source_event_id, "
                "created_at) VALUES(?,?,?,?,?,?)",
                (
                    finding_id, incident_key, finding_type, _json(details),
                    event.id, _now(),
                ),
            )
            conn.execute(
                "INSERT INTO finding_transitions"
                "(id, finding_id, from_status, to_status, source_event_id, "
                "actor, origin_evidence_type, created_at) "
                "VALUES(?,?,NULL,'active',?,?,?,?)",
                (
                    f"ftr_{uuid.uuid4().hex}", finding_id, event.id,
                    "integrity_monitor", EXTERNAL_UNTRUSTED, _now(),
                ),
            )
        return finding_id

    @integrity_checked
    def list_security_findings(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.*, (SELECT t.to_status FROM finding_transitions t "
                "WHERE t.finding_id=f.id ORDER BY t.rowid DESC LIMIT 1) status "
                "FROM security_findings f ORDER BY f.created_at"
            ).fetchall()
        return tuple(
            {
                "id": row["id"],
                "incident_key": row["incident_key"],
                "finding_type": row["finding_type"],
                "details": json.loads(row["details_json"]),
                "source_event_id": row["source_event_id"],
                "created_at": row["created_at"],
                "status": row["status"],
                "acknowledged": row["status"] == "acknowledged",
            }
            for row in rows
        )

    def append_finding_ack_request(
        self,
        finding_id: str,
        *,
        branch_id: str,
        origin_evidence_type: str,
    ) -> tuple[Event, str]:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id,to_status,source_event_id FROM finding_transitions "
                "WHERE finding_id=? ORDER BY rowid DESC LIMIT 1",
                (finding_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown security finding: {finding_id}")
            if str(row["to_status"]) == "acknowledgement_requested":
                existing = conn.execute(
                    "SELECT * FROM events WHERE id=?", (row["source_event_id"],)
                ).fetchone()
                assert existing is not None
                return self._event_from_row(existing), str(row["id"])
            if "acknowledgement_requested" not in FINDING_RULE.flow.get(
                str(row["to_status"]), frozenset()
            ):
                raise ValueError(
                    "illegal security finding transition: "
                    f"{row['to_status']} -> acknowledgement_requested"
                )
            event = self._append_event_in_tx(
                conn,
                branch_id=branch_id,
                session_id=None,
                kind="state",
                role=None,
                content=(
                    "security finding acknowledgement requested: "
                    f"{finding_id}"
                ),
                payload={
                    "operation": "acknowledgement_requested",
                    "finding_id": finding_id,
                    "origin_evidence_type": origin_evidence_type,
                },
                files=(),
            )
            transition_id = f"ftr_{uuid.uuid4().hex}"
            source_row = conn.execute(
                "SELECT * FROM events WHERE id=?", (event.id,)
            ).fetchone()
            assert source_row is not None
            validate_transition(
                FINDING_RULE,
                current=str(row["to_status"]),
                target="acknowledgement_requested",
                origin=self._event_origin_evidence(source_row),
                source_visible=self._event_visible_locked(conn, source_row, branch_id),
            )
            conn.execute(
                "INSERT INTO finding_transitions"
                "(id, finding_id, from_status, to_status, source_event_id, "
                "actor, origin_evidence_type, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transition_id, finding_id, row["to_status"],
                    "acknowledgement_requested", event.id, "request_reducer",
                    self._event_origin_evidence(source_row), _now(),
                ),
            )
        return event, transition_id
    def transition_finding(
        self,
        finding_id: str,
        to_status: str,
        *,
        source_event_id: str,
        actor: str,
        origin_evidence_type: str | None = None,
    ) -> str | None:
        if to_status not in {"acknowledgement_requested", "acknowledged"}:
            raise ValueError("unsupported finding transition")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT to_status FROM finding_transitions "
                "WHERE finding_id=? ORDER BY rowid DESC LIMIT 1",
                (finding_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown security finding: {finding_id}")
            source_row = conn.execute(
                "SELECT * FROM events WHERE id=?", (source_event_id,)
            ).fetchone()
            if source_row is None:
                raise KeyError(f"unknown source event: {source_event_id}")
            derived_origin = self._event_origin_evidence(source_row)
            decision = validate_transition(
                FINDING_RULE,
                current=str(row["to_status"]),
                target=to_status,
                origin=derived_origin,
                source_visible=self._event_visible_locked(conn, source_row, "main"),
                delegated_enabled=self._delegation_enabled_locked(conn),
            )
            if not decision.changed:
                return None
            transition_id = f"ftr_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO finding_transitions"
                "(id, finding_id, from_status, to_status, source_event_id, "
                "actor, origin_evidence_type, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transition_id, finding_id, row["to_status"], to_status,
                    source_event_id, actor, derived_origin, _now(),
                ),
            )
        return transition_id

    def activate_policy(
        self,
        policy: dict[str, Any],
        *,
        source_event_id: str,
        origin_evidence_type: str | None = None,
        operation: str = "activated",
    ) -> dict[str, Any]:
        if operation not in {"activated", "replaced", "rollback"}:
            raise ValueError("unsupported policy operation")
        with self._transaction() as conn:
            source_row = conn.execute(
                "SELECT * FROM events WHERE id=?", (source_event_id,)
            ).fetchone()
            if source_row is None:
                raise KeyError(f"unknown policy source event: {source_event_id}")
            derived_origin = self._event_origin_evidence(source_row)
            if origin_evidence_type is not None and origin_evidence_type != derived_origin:
                raise PermissionError("claimed origin evidence does not match source event")
            if derived_origin not in {BOOTSTRAP_TOFU, HOST_LOGICAL_USER}:
                raise PermissionError("policy activation requires trusted origin")
            previous = conn.execute(
                "SELECT id, version FROM policy_ledger "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
            version = int(previous["version"]) + 1 if previous else 1
            policy_id = f"policy_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO policy_ledger"
                "(id, version, policy_hash, policy_json, activation_event_id, "
                "previous_policy_id, operation, origin_evidence_type, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    policy_id, version, _hash(_json(policy)), _json(policy),
                    source_event_id, previous["id"] if previous else None,
                    operation, derived_origin, _now(),
                ),
            )
        return {
            "id": policy_id,
            "version": version,
            "policy_hash": _hash(_json(policy)),
            "policy": policy,
            "activation_event_id": source_event_id,
            "previous_policy_id": previous["id"] if previous else None,
            "operation": operation,
            "origin_evidence_type": derived_origin,
        }

    def active_policy(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM policy_ledger ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"], "version": int(row["version"]),
            "policy_hash": row["policy_hash"],
            "policy": json.loads(row["policy_json"]),
            "activation_event_id": row["activation_event_id"],
            "operation": row["operation"],
            "origin_evidence_type": row["origin_evidence_type"],
        }
    def register_extractor_config(
        self, config_hash: str, descriptor: dict[str, Any]
    ) -> None:
        if not config_hash:
            raise ValueError("extractor config hash must be non-empty")
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT descriptor_json FROM extractor_configs WHERE config_hash=?",
                (config_hash,),
            ).fetchone()
            encoded = _json(descriptor)
            if existing is not None and existing["descriptor_json"] != encoded:
                raise ValueError("extractor config hash collision")
            conn.execute(
                "INSERT OR IGNORE INTO extractor_configs"
                "(config_hash, descriptor_json, created_at) VALUES(?,?,?)",
                (config_hash, encoded, _now()),
            )

    @integrity_checked
    def preceding_canonical_events(self, seq: int, limit: int) -> list[Event]:
        if limit < 1:
            return []
        with self._lock:
            current = self._conn.execute(
                "SELECT branch_id, session_id FROM events WHERE seq=?", (seq,)
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown event seq: {seq}")
            rows = self._conn.execute(
                "SELECT * FROM events WHERE seq<? AND branch_id=? "
                "AND kind='message' AND role IN ('user','assistant') "
                "ORDER BY seq DESC LIMIT ?",
                (seq, current["branch_id"], limit),
            ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    @integrity_checked
    def pending_extraction_events(
        self,
        config_hash: str,
        *,
        limit: int | None = None,
        retry_failed: bool = False,
        max_retries: int = 3,
    ) -> list[Event]:
        query = (
            "SELECT e.* FROM events e "
            "LEFT JOIN extraction_runs r ON r.event_id=e.id "
            "AND r.extractor_config_hash=? "
            "WHERE e.kind='message' AND e.role IN ('user','assistant') "
            "AND (r.id IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM extraction_attempts a "
            "WHERE a.run_id=r.id AND a.outcome='succeeded'"
            ")) ORDER BY e.seq"
        )
        with self._lock:
            rows = self._conn.execute(query, (config_hash,)).fetchall()
            selected: list[sqlite3.Row] = []
            for row in rows:
                run = self._conn.execute(
                    "SELECT id FROM extraction_runs "
                    "WHERE event_id=? AND extractor_config_hash=?",
                    (row["id"], config_hash),
                ).fetchone()
                if run is None:
                    selected.append(row)
                    continue
                latest = self._conn.execute(
                    "SELECT outcome, attempt_no FROM extraction_attempts "
                    "WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
                    (run["id"],),
                ).fetchone()
                if latest is None:
                    selected.append(row)
                elif latest["outcome"] == "retryable_failure" and int(
                    latest["attempt_no"]
                ) < max_retries:
                    selected.append(row)
                elif retry_failed and latest["outcome"] in {
                    "retryable_failure", "terminal_failure"
                }:
                    selected.append(row)
            if limit is not None:
                selected = selected[: max(0, limit)]
        return [self._event_from_row(row) for row in selected]

    def ensure_extraction_run(self, event_id: str, config_hash: str) -> str:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id FROM extraction_runs "
                "WHERE event_id=? AND extractor_config_hash=?",
                (event_id, config_hash),
            ).fetchone()
            if row is not None:
                return str(row["id"])
            if conn.execute(
                "SELECT 1 FROM events WHERE id=?", (event_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown event: {event_id}")
            run_id = f"run_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO extraction_runs"
                "(id, event_id, extractor_config_hash, created_at) VALUES(?,?,?,?)",
                (run_id, event_id, config_hash, _now()),
            )
            return run_id

    def start_extraction_attempt(self, run_id: str) -> tuple[int, str]:
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM extraction_runs WHERE id=?", (run_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown extraction run: {run_id}")
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS value "
                "FROM extraction_attempt_starts WHERE run_id=?",
                (run_id,),
            ).fetchone()
            attempt_no = int(row["value"]) + 1
            started_at = _now()
            conn.execute(
                "INSERT INTO extraction_attempt_starts"
                "(id, run_id, attempt_no, started_at) VALUES(?,?,?,?)",
                (f"start_{uuid.uuid4().hex}", run_id, attempt_no, started_at),
            )
        return attempt_no, started_at

    def finish_extraction_failure(
        self,
        *,
        run_id: str,
        attempt_no: int,
        started_at: str,
        outcome: str,
        error_code: str,
        redacted_error: str,
    ) -> str:
        if outcome not in {"retryable_failure", "terminal_failure"}:
            raise ValueError("invalid extraction failure outcome")
        safe_error, _ = self.redactor.redact_text(redacted_error)
        attempt_id = f"attempt_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO extraction_attempts"
                "(id, run_id, attempt_no, outcome, started_at, finished_at, "
                "error_code, redacted_error, raw_response_ref) "
                "VALUES(?,?,?,?,?,?,?,?,NULL)",
                (
                    attempt_id, run_id, attempt_no, outcome, started_at, _now(),
                    str(error_code), safe_error[:2000],
                ),
            )
        return attempt_id

    @staticmethod
    def _normalized_key(value: str) -> str:
        return " ".join(value.strip().split()).casefold()

    def _existing_auto_memory_locked(
        self, conn: sqlite3.Connection, memory_type: str, normalized: str
    ) -> sqlite3.Row | None:
        rows = conn.execute(
            "SELECT * FROM memory_records WHERE memory_type=? ORDER BY created_at",
            (memory_type,),
        ).fetchall()
        key = self._normalized_key(normalized)
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            if self._normalized_key(row["content"]) == key:
                return row
        return None

    def commit_extraction_success(
        self,
        *,
        run_id: str,
        attempt_no: int,
        started_at: str,
        event: Event,
        candidates: Sequence[Any],
        rejections: Sequence[dict[str, Any]],
        raw_response: Any,
        extractor_config_hash: str,
    ) -> tuple[str, ...]:
        safe_raw, _ = self.redactor.redact_value(raw_response)
        try:
            raw_text = _json(safe_raw)
        except (TypeError, ValueError):
            raw_text, _ = self.redactor.redact_text(repr(safe_raw))
        attempt_id = f"attempt_{uuid.uuid4().hex}"
        raw_id = f"raw_{uuid.uuid4().hex}"
        created_candidate_ids: list[str] = []
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT event_id FROM extraction_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or run["event_id"] != event.id:
                raise ValueError("extraction run and canonical event do not match")
            conn.execute(
                "INSERT INTO extraction_attempts"
                "(id, run_id, attempt_no, outcome, started_at, finished_at, "
                "error_code, redacted_error, raw_response_ref) "
                "VALUES(?,?,?,?,?,?,NULL,NULL,?)",
                (
                    attempt_id, run_id, attempt_no, "succeeded",
                    started_at, _now(), raw_id,
                ),
            )
            conn.execute(
                "INSERT INTO extraction_raw_responses"
                "(id, attempt_id, encoding, payload, created_at) VALUES(?,?,?,?,?)",
                (
                    raw_id, attempt_id, "zlib+utf-8",
                    sqlite3.Binary(zlib.compress(raw_text.encode("utf-8"))), _now(),
                ),
            )
            for rejected in rejections:
                safe_error, _ = self.redactor.redact_text(
                    str(rejected.get("redacted_error", "validation failed"))
                )
                conn.execute(
                    "INSERT INTO extraction_rejections"
                    "(id, run_id, attempt_id, candidate_json, error_code, "
                    "redacted_error, created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        f"reject_{uuid.uuid4().hex}", run_id, attempt_id,
                        _json(rejected.get("candidate", {})),
                        str(rejected.get("error_code", "validation_failed")),
                        safe_error[:2000], _now(),
                    ),
                )
            interpretations: dict[tuple[str, int, int], set[str]] = {}
            for candidate in candidates:
                key = (
                    candidate.memory_type,
                    candidate.evidence_start,
                    candidate.evidence_end,
                )
                interpretations.setdefault(key, set()).add(
                    self._normalized_key(candidate.normalized_content)
                )
            conflicts = {
                key for key, values in interpretations.items() if len(values) > 1
            }
            for candidate in candidates:
                candidate_id = f"cand_{uuid.uuid4().hex}"
                candidate_key = (
                    candidate.memory_type,
                    candidate.evidence_start,
                    candidate.evidence_end,
                )
                initial_status = (
                    "quarantined"
                    if candidate_key in conflicts else candidate.initial_status
                )
                initial_rule = (
                    "conflicting_interpretations"
                    if candidate_key in conflicts else candidate.rule_id
                )
                created_at = _now()
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO extraction_candidates"
                    "(id, run_id, attempt_id, memory_type, normalized_content, "
                    "evidence_quote, evidence_start, evidence_end, evidence_zone, "
                    "confidence, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candidate_id, run_id, attempt_id, candidate.memory_type,
                        candidate.normalized_content, candidate.evidence_quote,
                        candidate.evidence_start, candidate.evidence_end,
                        candidate.evidence_zone, candidate.confidence, created_at,
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                created_candidate_ids.append(candidate_id)
                conn.execute(
                    "INSERT INTO candidate_transitions"
                    "(id, candidate_id, from_status, to_status, source_event_id, "
                    "actor, rule_id, origin_evidence_type, replacement_candidate_id, "
                    "replacement_memory_id, extractor_run_id, created_at) "
                    "VALUES(?,?,NULL,?,?,?,?,?,NULL,NULL,?,?)",
                    (
                        f"ctr_{uuid.uuid4().hex}", candidate_id,
                        initial_status, event.id, "extractor",
                    initial_rule, origin_evidence_type(event), run_id, created_at,
                    ),
                )
                if initial_status != "auto":
                    continue
                existing = self._existing_auto_memory_locked(
                    conn, candidate.memory_type, candidate.normalized_content
                )
                if existing is not None:
                    memory_id = str(existing["id"])
                    relation = "supports"
                else:
                    memory_id = f"mem_{uuid.uuid4().hex}"
                    summary = candidate.normalized_content[:240]
                    derivation = self._append_event_in_tx(
                        conn,
                        branch_id=event.branch_id,
                        session_id=None,
                        kind="state",
                        role=None,
                        content=f"auto-derived {candidate.memory_type}: {summary}",
                        payload={
                            "operation": "auto_derive_memory",
                            "memory_id": memory_id,
                            "source_event_ids": [event.id],
                            "extraction_run_id": run_id,
                            "candidate_id": candidate_id,
                            "extractor_config_hash": extractor_config_hash,
                        },
                        files=event.files,
                    )
                    metadata = {
                        "origin": "auto",
                        "authority_level": "auto",
                        "origin_evidence_type": "extractor",
                        "extraction_run_id": run_id,
                        "candidate_id": candidate_id,
                        "extractor_config_hash": extractor_config_hash,
                        "confidence": candidate.confidence,
                        "evidence_zone": candidate.evidence_zone,
                    }
                    conn.execute(
                        "INSERT INTO memory_records"
                        "(id, branch_id, memory_type, content, summary, files_json, "
                        "risk, retrieval_cost, version, source_event_ids_json, "
                        "supersedes_id, cursor_seq, created_at, metadata_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
                        (
                            memory_id, event.branch_id, candidate.memory_type,
                            candidate.normalized_content, summary, _json(event.files),
                            0.0, 1.25, 1, _json([event.id]), derivation.seq,
                            created_at, _json(metadata),
                        ),
                    )
                    if self.fts_enabled:
                        conn.execute(
                            "INSERT INTO memories_fts(memory_id, content, summary, signals) "
                            "VALUES(?,?,?,?)",
                            (
                                memory_id, candidate.normalized_content, summary,
                                _memory_signals(
                                    getattr(candidate, "valid_from", None),
                                    getattr(candidate, "valid_to", None),
                                    tuple(event.files),
                                ),
                            ),
                        )
                    relation = "derived"
                conn.execute(
                    "INSERT INTO candidate_memory_links"
                    "(id, candidate_id, memory_id, relation, source_event_id, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        f"cml_{uuid.uuid4().hex}", candidate_id, memory_id,
                        relation, event.id, created_at,
                    ),
                )
        return tuple(created_candidate_ids)

    def _candidate_status_locked(
        self, conn: sqlite3.Connection, candidate_id: str
    ) -> str | None:
        row = conn.execute(
            "SELECT to_status FROM candidate_transitions "
            "WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        return str(row["to_status"]) if row else None

    @integrity_checked
    def list_extraction_candidates(
        self, *, status: str | None = None
    ) -> tuple[ExtractionCandidate, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.*, (SELECT t.to_status FROM candidate_transitions t "
                "WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1) "
                "AS current_status FROM extraction_candidates c ORDER BY c.created_at"
            ).fetchall()
        values = [
            ExtractionCandidate(
                id=row["id"], run_id=row["run_id"], attempt_id=row["attempt_id"],
                memory_type=row["memory_type"],
                normalized_content=row["normalized_content"],
                evidence_quote=row["evidence_quote"],
                evidence_start=int(row["evidence_start"]),
                evidence_end=int(row["evidence_end"]),
                evidence_zone=row["evidence_zone"],
                confidence=float(row["confidence"]),
                created_at=row["created_at"],
                current_status=row["current_status"],
            )
            for row in rows
        ]
        if status is not None:
            values = [item for item in values if item.current_status == status]
        return tuple(values)

    def append_candidate_request(
        self,
        candidate_id: str,
        request_status: str,
        *,
        branch_id: str,
        action: str,
        origin_evidence_type: str,
        replacement_candidate_id: str | None = None,
        replacement_memory_id: str | None = None,
    ) -> tuple[Event, str]:
        allowed = {
            "confirmation_requested", "rejection_requested",
            "supersession_requested",
        }
        if request_status not in allowed:
            raise ValueError("unsupported candidate request")
        with self._transaction() as conn:
            current = self._candidate_status_locked(conn, candidate_id)
            if current is None:
                raise KeyError(f"unknown extraction candidate: {candidate_id}")
            latest = conn.execute(
                "SELECT id,source_event_id FROM candidate_transitions "
                "WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if current == request_status:
                assert latest is not None
                existing = conn.execute(
                    "SELECT * FROM events WHERE id=?", (latest["source_event_id"],)
                ).fetchone()
                assert existing is not None
                return self._event_from_row(existing), str(latest["id"])
            target = conn.execute(
                "SELECT e.branch_id FROM extraction_candidates c "
                "JOIN extraction_runs r ON r.id=c.run_id "
                "JOIN events e ON e.id=r.event_id WHERE c.id=?",
                (candidate_id,),
            ).fetchone()
            if target is None:
                raise StoreIntegrityError("candidate has no source event")
            event = self._append_event_in_tx(
                conn,
                branch_id=branch_id,
                session_id=None,
                kind="state",
                role=None,
                content=f"candidate {action} requested: {candidate_id}",
                payload={
                    "operation": request_status,
                    "candidate_id": candidate_id,
                    "replacement_candidate_id": replacement_candidate_id,
                    "replacement_memory_id": replacement_memory_id,
                    "origin_evidence_type": origin_evidence_type,
                },
                files=(),
            )
            transition_id = f"ctr_{uuid.uuid4().hex}"
            source_row = conn.execute(
                "SELECT * FROM events WHERE id=?", (event.id,)
            ).fetchone()
            assert source_row is not None
            validate_transition(
                CANDIDATE_RULE,
                current=current,
                target=request_status,
                origin=self._event_origin_evidence(source_row),
                source_visible=self._event_visible_locked(
                    conn, source_row, str(target["branch_id"])
                ),
            )
            conn.execute(
                "INSERT INTO candidate_transitions"
                "(id, candidate_id, from_status, to_status, source_event_id, "
                "actor, rule_id, origin_evidence_type, replacement_candidate_id, "
                "replacement_memory_id, extractor_run_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)",
                (
                    transition_id, candidate_id, current, request_status,
                    event.id, "request_reducer", f"untrusted_{action}_request",
                    self._event_origin_evidence(source_row), replacement_candidate_id,
                    replacement_memory_id, _now(),
                ),
            )
        return event, transition_id
    def transition_candidate(
        self,
        candidate_id: str,
        to_status: str,
        *,
        source_event_id: str,
        actor: str,
        rule_id: str,
        origin_evidence_type: str | None = None,
        replacement_candidate_id: str | None = None,
        replacement_memory_id: str | None = None,
    ) -> str | None:
        allowed = set(CANDIDATE_RULE.flow)
        if to_status not in allowed:
            raise ValueError(f"unsupported candidate status: {to_status}")
        if to_status == "superseded" and not (
            replacement_candidate_id or replacement_memory_id
        ):
            raise ValueError("superseded transition requires a replacement")
        transition_id = f"ctr_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            current = self._candidate_status_locked(conn, candidate_id)
            if current is None:
                raise KeyError(f"unknown extraction candidate: {candidate_id}")
            target = conn.execute(
                "SELECT e.branch_id FROM extraction_candidates c "
                "JOIN extraction_runs r ON r.id=c.run_id "
                "JOIN events e ON e.id=r.event_id WHERE c.id=?",
                (candidate_id,),
            ).fetchone()
            if target is None:
                raise StoreIntegrityError("candidate has no source event")
            source_row = conn.execute(
                "SELECT * FROM events WHERE id=?", (source_event_id,)
            ).fetchone()
            if source_row is None:
                raise KeyError(f"unknown source event: {source_event_id}")
            derived_origin = self._event_origin_evidence(source_row)
            decision = validate_transition(
                CANDIDATE_RULE,
                current=current,
                target=to_status,
                origin=derived_origin,
                source_visible=self._event_visible_locked(
                    conn, source_row, str(target["branch_id"])
                ),
                delegated_enabled=self._delegation_enabled_locked(conn),
            )
            if not decision.changed:
                return None
            conn.execute(
                "INSERT INTO candidate_transitions"
                "(id, candidate_id, from_status, to_status, source_event_id, "
                "actor, rule_id, origin_evidence_type, replacement_candidate_id, "
                "replacement_memory_id, extractor_run_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)",
                (
                    transition_id, candidate_id, current, to_status,
                    source_event_id, actor, rule_id, derived_origin,
                    replacement_candidate_id, replacement_memory_id, _now(),
                ),
            )
        return transition_id

    # --- candidate settlement (task6.md 6B) --------------------------------
    # Thin primitives only; policy and semantics live in settlement.py /
    # reconciler.py. Settlement candidates ride the extraction ledger with
    # candidate_kind != 'extraction' and a settlement-specific status
    # vocabulary: pending -> applied | contested; applied -> reverted |
    # contested. Consume-once: repeats are idempotent, conflicts fail closed.

    _SETTLEMENT_STATUSES = set(SETTLEMENT_FLOW)
    _SETTLEMENT_FLOW = SETTLEMENT_FLOW

    @integrity_checked
    def create_settlement_candidate(
        self,
        *,
        kind: str,
        content: str,
        source_event_id: str,
        evidence_event_id: str | None = None,
        memory_type: str = "task",
        strength: str = "weak",
        actor: str = "reconciler",
    ) -> tuple[str, bool, str]:
        """Create (or find) the settlement candidate anchored to one source
        event. Returns (candidate_id, created, current_status). The synthetic
        run row keyed by the source event keeps the ledger's foreign keys and
        uniqueness honest: one detection -> one run -> one candidate."""
        if kind == "extraction":
            raise ValueError("extraction candidates are created by the extractor")
        created_at = _now()
        with self._transaction() as conn:
            run_id = f"run_settle_{hashlib.sha256(source_event_id.encode()).hexdigest()[:24]}"
            conn.execute(
                "INSERT OR IGNORE INTO extraction_runs"
                "(id, event_id, extractor_config_hash, created_at) VALUES(?,?,?,?)",
                (run_id, source_event_id, SETTLEMENT_CONFIG_HASH, created_at),
            )
            attempt_id = f"att_{run_id[12:]}"
            conn.execute(
                "INSERT OR IGNORE INTO extraction_attempts"
                "(id, run_id, attempt_no, outcome, started_at, finished_at) "
                "VALUES(?,?,1,'deterministic',?,?)",
                (attempt_id, run_id, created_at, created_at),
            )
            candidate_id = f"cand_{uuid.uuid4().hex}"
            cursor = conn.execute(
                "INSERT OR IGNORE INTO extraction_candidates"
                "(id, run_id, attempt_id, memory_type, normalized_content, "
                "evidence_quote, evidence_start, evidence_end, evidence_zone, "
                "confidence, created_at, candidate_kind) "
                "VALUES(?,?,?,?,?,?,0,0,?,1.0,?,?)",
                (
                    candidate_id, run_id, attempt_id, memory_type, content,
                    evidence_event_id or source_event_id, f"{kind}:{strength}",
                    created_at, kind,
                ),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT c.id, (SELECT t.to_status FROM candidate_transitions t "
                    "WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1) AS status "
                    "FROM extraction_candidates c WHERE c.run_id=? AND "
                    "c.normalized_content=? AND c.candidate_kind=?",
                    (run_id, content, kind),
                ).fetchone()
                if row is None:
                    raise StoreIntegrityError(
                        "settlement candidate uniqueness violated without a match"
                    )
                return str(row["id"]), False, str(row["status"] or "pending")
            conn.execute(
                "INSERT INTO candidate_transitions"
                "(id, candidate_id, from_status, to_status, source_event_id, "
                "actor, rule_id, origin_evidence_type, replacement_candidate_id, "
                "replacement_memory_id, extractor_run_id, created_at) "
                "VALUES(?,?,NULL,'pending',?,?,?,?,NULL,NULL,?,?)",
                (
                    f"ctr_{uuid.uuid4().hex}", candidate_id, source_event_id,
                    actor, f"{kind}_detected",
                    self._event_origin_evidence(
                        conn.execute(
                            "SELECT * FROM events WHERE id=?", (source_event_id,)
                        ).fetchone()
                    ),
                    run_id, created_at,
                ),
            )
        return candidate_id, True, "pending"

    @integrity_checked
    def settle_candidate(
        self,
        candidate_id: str,
        to_status: str,
        *,
        source_event_id: str,
        actor: str,
        rule_id: str,
    ) -> str | None:
        """Consume-once settlement transition. Idempotent on repeats (returns
        None), fail-closed on conflicts (ValueError). Non-system actors are
        trust-hardened (task6.md 6C): the cited source event must derive a
        trusted origin, and a delegated agent additionally needs the active
        policy to enable agent settlement."""
        if to_status not in self._SETTLEMENT_STATUSES:
            raise ValueError(f"unsupported settlement status: {to_status}")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT c.candidate_kind,e.branch_id FROM extraction_candidates c "
                "JOIN extraction_runs r ON r.id=c.run_id "
                "JOIN events e ON e.id=r.event_id WHERE c.id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown candidate: {candidate_id}")
            if str(row["candidate_kind"]) == "extraction":
                raise ValueError(
                    "extraction candidates settle through transition_candidate"
                )
            source_row = conn.execute(
                "SELECT * FROM events WHERE id=?", (source_event_id,)
            ).fetchone()
            if source_row is None:
                raise KeyError(f"unknown source event: {source_event_id}")
            recorded_origin = self._event_origin_evidence(source_row)
            current = self._candidate_status_locked(conn, candidate_id) or "pending"
            decision = validate_transition(
                SETTLEMENT_RULE,
                current=current,
                target=to_status,
                origin=recorded_origin,
                source_visible=self._event_visible_locked(
                    conn, source_row, str(row["branch_id"])
                ),
                delegated_enabled=self._delegation_enabled_locked(conn),
                system_actor=actor == "system",
            )
            if not decision.changed:
                return None
            transition_id = f"ctr_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO candidate_transitions"
                "(id, candidate_id, from_status, to_status, source_event_id, "
                "actor, rule_id, origin_evidence_type, replacement_candidate_id, "
                "replacement_memory_id, extractor_run_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,?)",
                (
                    transition_id, candidate_id, current, to_status,
                    source_event_id, actor, rule_id, recorded_origin, _now(),
                ),
            )
        return transition_id

    @integrity_checked
    def list_settlement_candidates(
        self, *, kind: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["c.candidate_kind != 'extraction'"]
        params: list[Any] = []
        if kind is not None:
            clauses.append("c.candidate_kind = ?")
            params.append(kind)
        rows = self._conn.execute(
            "SELECT c.*, r.event_id AS source_event_id, "
            "(SELECT t.to_status FROM candidate_transitions t "
            "WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1) AS status, "
            "(SELECT t.created_at FROM candidate_transitions t "
            "WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1) AS status_at "
            "FROM extraction_candidates c "
            "JOIN extraction_runs r ON r.id = c.run_id "
            "WHERE " + " AND ".join(clauses)
            + " ORDER BY c.created_at DESC",
            params,
        ).fetchall()
        results = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            if status is not None and (item.get("status") or "pending") != status:
                continue
            results.append(item)
        return results

    @integrity_checked
    def get_settlement_candidate(self, candidate_id: str) -> dict[str, Any]:
        """One candidate plus its full transition history — the audit view
        behind `candidates show` and the MCP read tool (task6.md 6C)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT c.*, r.event_id AS source_event_id "
                "FROM extraction_candidates c "
                "JOIN extraction_runs r ON r.id = c.run_id "
                "WHERE c.id=? AND c.candidate_kind != 'extraction'",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown settlement candidate: {candidate_id}")
            transition_rows = self._conn.execute(
                "SELECT * FROM candidate_transitions WHERE candidate_id=? "
                "ORDER BY rowid",
                (candidate_id,),
            ).fetchall()
        candidate = {key: row[key] for key in row.keys()}
        transitions = [
            {key: item[key] for key in item.keys()} for item in transition_rows
        ]
        candidate["status"] = (
            transitions[-1]["to_status"] if transitions else "pending"
        )
        candidate["transitions"] = transitions
        return candidate

    @integrity_checked
    def recent_settlement_transitions(
        self, *, to_status: str, since_iso: str, kind: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT t.*, c.normalized_content, c.candidate_kind, c.evidence_quote "
            "FROM candidate_transitions t "
            "JOIN extraction_candidates c ON c.id = t.candidate_id "
            "WHERE t.to_status=? AND t.created_at>=? AND c.candidate_kind != 'extraction' "
            "ORDER BY t.created_at DESC",
            (to_status, since_iso),
        ).fetchall()
        return [
            {key: row[key] for key in row.keys()}
            for row in rows
            if kind is None or str(row["candidate_kind"]) == kind
        ]

    @integrity_checked
    def find_auto_candidate_match(
        self, memory_type: str, content: str
    ) -> tuple[str, str] | None:
        key = self._normalized_key(content)
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.normalized_content, l.memory_id, "
                "(SELECT t.to_status FROM candidate_transitions t "
                "WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1) AS status "
                "FROM extraction_candidates c "
                "JOIN candidate_memory_links l ON l.candidate_id=c.id "
                "WHERE c.memory_type=? ORDER BY c.created_at",
                (memory_type,),
            ).fetchall()
        for row in rows:
            if (
                row["status"] in {
                    "auto", "confirmation_requested", "confirmed"
                }
                and self._normalized_key(row["normalized_content"]) == key
            ):
                return str(row["id"]), str(row["memory_id"])
        return None

    def confirm_candidate_match(
        self,
        candidate_id: str,
        memory_id: str,
        *,
        source_event_id: str,
    ) -> None:
        current = next(
            (
                item.current_status
                for item in self.list_extraction_candidates()
                if item.id == candidate_id
            ),
            None,
        )
        if current == "auto":
            self.transition_candidate(
                candidate_id,
                "confirmation_requested",
                source_event_id=source_event_id,
                actor="explicit_marker",
                rule_id="normalized_explicit_match_requested",
            )
        self.transition_candidate(
            candidate_id,
            "confirmed",
            source_event_id=source_event_id,
            actor="explicit_marker",
            rule_id="normalized_explicit_match",
        )
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO candidate_memory_links"
                "(id, candidate_id, memory_id, relation, source_event_id, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"cml_{uuid.uuid4().hex}", candidate_id, memory_id,
                    "confirmed_as", source_event_id, _now(),
                ),
            )

    @integrity_checked
    def memory_authority(self, memory_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 AS confirmed FROM candidate_memory_links l "
                "WHERE l.memory_id=? AND ("
                "SELECT t.to_status FROM candidate_transitions t "
                "WHERE t.candidate_id=l.candidate_id ORDER BY t.rowid DESC LIMIT 1"
                ")='confirmed' LIMIT 1",
                (memory_id,),
            ).fetchone()
        if row:
            return "confirmed"
        record = self.get_memory(memory_id)
        return str(record.metadata.get("authority_level", "confirmed"))

    @integrity_checked
    def extraction_status(self, config_hash: str | None) -> dict[str, Any]:
        with self._lock:
            if config_hash is None:
                pending_rows: list[sqlite3.Row] = []
                failed = 0
                retries = 0
                last_success = None
            else:
                pending_rows = self._conn.execute(
                    "SELECT e.created_at FROM events e "
                    "LEFT JOIN extraction_runs r ON r.event_id=e.id "
                    "AND r.extractor_config_hash=? "
                    "WHERE e.kind='message' AND e.role IN ('user','assistant') "
                    "AND (r.id IS NULL OR NOT EXISTS ("
                    "SELECT 1 FROM extraction_attempts a "
                    "WHERE a.run_id=r.id AND a.outcome='succeeded'))",
                    (config_hash,),
                ).fetchall()
                failed = int(self._conn.execute(
                    "SELECT COUNT(DISTINCT r.event_id) AS value "
                    "FROM extraction_runs r JOIN extraction_attempts a ON a.run_id=r.id "
                    "WHERE r.extractor_config_hash=? "
                    "AND a.outcome='terminal_failure'",
                    (config_hash,),
                ).fetchone()["value"])
                retries = int(self._conn.execute(
                    "SELECT COUNT(*) AS value FROM extraction_attempts a "
                    "JOIN extraction_runs r ON r.id=a.run_id "
                    "WHERE r.extractor_config_hash=? AND a.attempt_no>1",
                    (config_hash,),
                ).fetchone()["value"])
                row = self._conn.execute(
                    "SELECT MAX(a.finished_at) AS value FROM extraction_attempts a "
                    "JOIN extraction_runs r ON r.id=a.run_id "
                    "WHERE r.extractor_config_hash=? AND a.outcome='succeeded'",
                    (config_hash,),
                ).fetchone()
                last_success = row["value"]
            quarantined = self._conn.execute(
                "SELECT c.created_at FROM extraction_candidates c "
                "WHERE (SELECT t.to_status FROM candidate_transitions t "
                "WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1)"
                "='quarantined'"
            ).fetchall()
        now = datetime.now(UTC)
        def oldest_age(rows: Sequence[sqlite3.Row]) -> float | None:
            if not rows:
                return None
            values = []
            for row in rows:
                try:
                    values.append((now - datetime.fromisoformat(row["created_at"])).total_seconds())
                except ValueError:
                    pass
            return max(values) if values else None
        return {
            "pending_events": len(pending_rows),
            "oldest_pending_age": oldest_age(pending_rows),
            "failed_events": failed,
            "last_success_at": last_success,
            "retry_count": retries,
            "quarantined_candidates": len(quarantined),
            "oldest_quarantined_age": oldest_age(quarantined),
        }

    def signal_extraction_worker(self, config_hash: str) -> int:
        """Durably coalesce extraction wakeups into a monotonic generation."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO extraction_wakeups"
                "(config_hash, generation, owner, lease_until, updated_at) "
                "VALUES(?,1,NULL,NULL,?) "
                "ON CONFLICT(config_hash) DO UPDATE SET "
                "generation=extraction_wakeups.generation+1, updated_at=excluded.updated_at",
                (config_hash, _now()),
            )
            row = conn.execute(
                "SELECT generation FROM extraction_wakeups WHERE config_hash=?",
                (config_hash,),
            ).fetchone()
        return int(row["generation"])

    def claim_extraction_worker(
        self,
        config_hash: str,
        owner: str,
        *,
        lease_seconds: float = 300.0,
    ) -> int | None:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT generation, owner, lease_until FROM extraction_wakeups "
                "WHERE config_hash=?",
                (config_hash,),
            ).fetchone()
            if row is None:
                return None
            if (
                row["owner"] is not None
                and row["owner"] != owner
                and float(row["lease_until"] or 0) > now
            ):
                return None
            conn.execute(
                "UPDATE extraction_wakeups SET owner=?, lease_until=?, updated_at=? "
                "WHERE config_hash=?",
                (owner, now + lease_seconds, _now(), config_hash),
            )
            return int(row["generation"])

    def complete_extraction_worker_cycle(
        self,
        config_hash: str,
        owner: str,
        observed_generation: int,
        *,
        lease_seconds: float = 300.0,
    ) -> tuple[bool, int]:
        """Release a stable generation or renew the lease when another wakeup arrived."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT generation, owner FROM extraction_wakeups WHERE config_hash=?",
                (config_hash,),
            ).fetchone()
            if row is None or row["owner"] != owner:
                return True, observed_generation
            generation = int(row["generation"])
            if generation == observed_generation:
                conn.execute(
                    "UPDATE extraction_wakeups SET owner=NULL, lease_until=NULL, updated_at=? "
                    "WHERE config_hash=? AND owner=?",
                    (_now(), config_hash, owner),
                )
                return True, generation
            conn.execute(
                "UPDATE extraction_wakeups SET lease_until=?, updated_at=? "
                "WHERE config_hash=? AND owner=?",
                (time.time() + lease_seconds, _now(), config_hash, owner),
            )
            return False, generation

    def verify_chain(self) -> tuple[bool, str | None]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        previous: str | None = None
        for row in rows:
            values = {
                "id": row["id"], "branch_id": row["branch_id"],
                "session_id": row["session_id"], "kind": row["kind"],
                "role": row["role"], "content": row["content"],
                "payload": json.loads(row["payload_json"]),
                "files": json.loads(row["files_json"]), "created_at": row["created_at"],
            }
            if row["origin_channel"] != "legacy_untrusted":
                values["origin_channel"] = row["origin_channel"]
                values["origin_adapter"] = row["origin_adapter"]
            canonical = _json(values)
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

    @integrity_checked
    def storage_breakdown(self) -> dict[str, int]:
        groups = {
            "canonical_data": (
                "events", "artifacts", "branches", "sessions",
                "hook_sessions", "ingest_receipts",
            ),
            "interpretation_ledger": (
                "extractor_configs", "extraction_runs",
                "extraction_attempt_starts", "extraction_attempts",
                "extraction_candidates", "extraction_rejections",
                "candidate_transitions", "candidate_memory_links",
                "memory_records", "policy_ledger", "security_findings",
                "finding_transitions", "usage_samples", "snapshot_prunings",
            ),
            "raw_extractor_payloads": ("extraction_raw_responses",),
            "rebuildable_projections": (
                "events_fts", "memories_fts", "tool_output_views",
                "snapshots", "consolidation_receipts", "extraction_wakeups",
            ),
        }
        result: dict[str, int] = {}
        with self._lock:
            existing = {
                row["name"] for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            for group, tables in groups.items():
                total = 0
                for table in tables:
                    if table not in existing:
                        continue
                    for row in self._conn.execute(f"SELECT * FROM {table}"):
                        for value in row:
                            if value is None:
                                continue
                            if isinstance(value, bytes):
                                total += len(value)
                            else:
                                total += len(str(value).encode("utf-8"))
                result[group] = total
        result["database_file_bytes"] = self.database_size()
        return result
    def database_size(self) -> int:
        if self._in_memory:
            return 0
        total = self.path.stat().st_size if self.path.exists() else 0
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total
