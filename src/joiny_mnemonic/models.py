from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_OUTPUT = "tool_output"
    ARTIFACT = "artifact"
    STATE = "state"
    MEMORY_BLOCK = "memory_block"


class MemoryType(StrEnum):
    FACT = "fact"
    DECISION = "decision"
    TASK = "task"
    PREFERENCE = "preference"
    FAILURE = "failure"
    LESSON = "lesson"
    SUMMARY = "summary"
    INDEX = "index"


class BlockName(StrEnum):
    INSTRUCTIONS = "instructions"
    GOAL = "goal"
    CONSTRAINTS = "constraints"
    DECISIONS = "decisions"
    OPEN_TASKS = "open_tasks"


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    id: str
    branch_id: str
    session_id: str | None
    kind: str
    role: str | None
    origin_channel: str
    origin_adapter: str | None
    content: str
    payload: dict[str, Any]
    files: tuple[str, ...]
    created_at: str
    previous_hash: str | None
    content_hash: str
    chain_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    event_id: str
    name: str
    mime_type: str
    content_hash: str
    data: bytes
    created_at: str


@dataclass(frozen=True, slots=True)
class ActiveBlock:
    id: str
    branch_id: str
    name: str
    content: str
    version: int
    source_event_ids: tuple[str, ...]
    supersedes_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    branch_id: str
    memory_type: str
    content: str
    summary: str
    files: tuple[str, ...]
    risk: float
    retrieval_cost: float
    version: int
    source_event_ids: tuple[str, ...]
    supersedes_id: str | None
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    valid_from: str | None = None
    valid_to: str | None = None
    valid_from_precision: str | None = None
    valid_to_precision: str | None = None
    temporal_expression: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionCandidate:
    id: str
    run_id: str
    attempt_id: str
    memory_type: str
    normalized_content: str
    evidence_quote: str
    evidence_start: int
    evidence_end: int
    evidence_zone: str
    confidence: float
    created_at: str
    current_status: str


@dataclass(frozen=True, slots=True)
class ExtractionStatus:
    extractor_available: bool
    extractor_enabled: bool
    extractor_name: str | None
    extractor_config_hash: str | None
    pending_events: int
    oldest_pending_age: float | None
    failed_events: int
    last_success_at: str | None
    retry_count: int
    quarantined_candidates: int
    oldest_quarantined_age: float | None

@dataclass(frozen=True, slots=True)
class RetrievalHit:
    id: str
    source_kind: str
    memory_type: str
    representation: str
    content: str
    score: float
    source_event_ids: tuple[str, ...]
    files: tuple[str, ...]
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: str
    branch_id: str
    parent_snapshot_id: str | None
    cursor_seq: int
    state: dict[str, Any]
    project: dict[str, Any]
    created_at: str
    state_format: str = "json-patch-v2"
    state_sha256: str | None = None
    replay_code_version: str | None = None
    blob_available: bool = True


@dataclass(frozen=True, slots=True)
class PromptPacket:
    text: str
    estimated_tokens: int
    token_budget: int
    included_event_ids: tuple[str, ...]
    included_memory_ids: tuple[str, ...]
    snapshot_id: str | None = None
    stale_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolOutputView:
    id: str
    event_id: str
    level: str
    reducer: str
    reducer_version: str
    content: str
    source_hash: str
    content_hash: str
    raw_bytes: int
    view_bytes: int
    raw_tokens: int
    view_tokens: int
    latency_ns: int
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class UsageSample:
    id: str
    branch_id: str
    session_id: str | None
    event_id: str | None
    source: str
    operation: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    context_tokens: int
    estimated: bool
    cost_usd: float | None
    latency_ms: float | None
    raw_bytes: int
    emitted_bytes: int
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    id: str
    branch_id: str
    version: int
    context_window_tokens: int
    snapshot_ratio: float
    compact_ratio: float
    handoff_ratio: float
    hard_limit_ratio: float
    min_action_interval_events: int
    created_at: str
    agent: str | None = None
    profile: str | None = None
    recommended_handoff_tokens: int | None = None
    reserve_tokens: int = 0
    source: str = "database"


@dataclass(frozen=True, slots=True)
class GovernorDecision:
    branch_id: str
    context_tokens: int
    context_ratio: float
    actions: tuple[str, ...]
    reasons: tuple[str, ...]
    policy_id: str
    source: str


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    task_key: str
    branch_id: str
    version: int
    title: str
    status: str
    parent_task_key: str | None
    source_event_ids: tuple[str, ...]
    snapshot_id: str | None
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    agent: str
    hooks: frozenset[str] = frozenset()
    kv_access: bool = False
    artifact_access: bool = True
    lifecycle_events: bool = True

    def supports(self, capability: str) -> bool:
        if capability == "kv_access":
            return self.kv_access
        if capability == "artifact_access":
            return self.artifact_access
        if capability == "lifecycle_events":
            return self.lifecycle_events
        return capability in self.hooks
