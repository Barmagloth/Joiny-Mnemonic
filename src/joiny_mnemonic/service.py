from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Sequence

from .adapters import ADAPTERS, adapter_capabilities, get_adapter
from .code_index import PythonCodeIndex
from .consolidation import CompactionResult, ConsolidationResult, EvidenceConsolidator
from .context_limits import ContextLimitConfig
from .models import BudgetPolicy, Event, MemoryRecord, PromptPacket, RetrievalHit, Snapshot, ToolOutputView
from .governor import BudgetGovernor
from .plugins import PluginRegistry
from .prompt import PromptAssembler
from .reducers import ReductionBundle, ToolOutputReducer, materialize_view
from .retrieval import RetrievalContext, RetrievalEngine
from .snapshots import SnapshotManager
from .storage import MemoryStore
from .tasks import TaskManager
from .usage import UsageMeter


DURABLE_MEMORY_INSTRUCTION = (
    "<durable_memory_capture>\n"
    "[DURABLE MEMORY CAPTURE]\n"
    "For information that must survive future sessions, use a structured memory tool when "
    "available; otherwise emit one concise standalone Goal:, Decision:, Fact:, Constraint:, "
    "TODO:, or Preference: line. Mark only durable, evidence-backed items. Unmarked prose remains "
    "searchable but is not guaranteed in compact resume.\n"
    "</durable_memory_capture>"
)


class MemoryService:
    """One agent-neutral core shared by the CLI, HTTP API, MCP, and adapters."""

    def __init__(
        self,
        database: str | Path,
        *,
        project_root: str | Path = ".",
        plugins: PluginRegistry | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = MemoryStore(database)
        self.context_limits = ContextLimitConfig(self.project_root)
        self.plugins = plugins or PluginRegistry()
        self.retrieval = RetrievalEngine(self.store, self.plugins)
        self.snapshots = SnapshotManager(self.store, self.project_root)
        self.prompts = PromptAssembler(self.store, self.retrieval)
        self.consolidator = EvidenceConsolidator()
        self.code = PythonCodeIndex(self.project_root)
        self.reducer = ToolOutputReducer()
        self.usage = UsageMeter(self.store)
        self.tasks = TaskManager(self)
        self.governor = BudgetGovernor(self)
        self.plugin_errors = self.plugins.errors

    def budget_policy(
        self, *, branch_id: str = "main", agent: str | None = None
    ) -> BudgetPolicy:
        if agent:
            configured = self.context_limits.resolve(agent, branch_id=branch_id)
            if configured is not None:
                return configured
        return self.store.get_budget_policy(branch_id=branch_id)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> MemoryService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def ingest_native(
        self,
        agent: str,
        native_event: dict[str, Any],
        *,
        branch_id: str = "main",
        session_id: str | None = None,
    ) -> Event | None:
        adapter = get_adapter(agent)
        if adapter is None:
            raise KeyError(f"unknown adapter: {agent}")
        normalized = adapter.normalize(native_event)
        if normalized is None:
            return None
        event = self.store.append_event(
            kind=normalized.kind,
            content=normalized.content,
            role=normalized.role,
            payload=normalized.payload,
            files=normalized.files,
            branch_id=branch_id,
            session_id=session_id,
        )
        if event.kind == "tool_output":
            self.reduce_tool_output(event)
        self.usage.capture_native(
            native_event,
            source=agent,
            branch_id=branch_id,
            session_id=session_id,
            event_id=event.id,
        )
        self.consolidator.consolidate_event(self, event)
        return event

    def reduce_tool_output(
        self, event: Event
    ) -> tuple[ReductionBundle, tuple[ToolOutputView, ...]]:
        bundle = self.reducer.reduce(event)
        stored: list[ToolOutputView] = []
        for view in bundle.views:
            stored.append(self.store.save_tool_output_view(**materialize_view(event, view, bundle)))
        compact = next((view for view in stored if view.level == "compact"), None)
        emitted_tokens = compact.view_tokens if compact is not None else bundle.raw_tokens
        emitted_bytes = compact.view_bytes if compact is not None else bundle.raw_bytes
        self.usage.record_reduction(
            event, bundle, emitted_tokens=emitted_tokens, emitted_bytes=emitted_bytes
        )
        return bundle, tuple(stored)

    def reduce_tool_outputs(
        self, events: Sequence[Event]
    ) -> tuple[tuple[ReductionBundle, tuple[ToolOutputView, ...]], ...]:
        return tuple(
            self.reduce_tool_output(event) for event in events if event.kind == "tool_output"
        )

    def consolidate(
        self,
        *,
        branch_id: str = "main",
    ) -> tuple[ConsolidationResult, ...]:
        return self.consolidator.consolidate_pending(self, branch_id=branch_id)

    def compact(
        self,
        *,
        branch_id: str = "main",
        keep_recent_groups: int = 8,
        summary_budget: int = 600,
    ) -> CompactionResult:
        self.consolidate(branch_id=branch_id)
        return self.consolidator.compact(
            self,
            branch_id=branch_id,
            keep_recent_groups=keep_recent_groups,
            summary_budget=summary_budget,
        )

    def derive_memory(self, **values: Any) -> MemoryRecord:
        record = self.store.derive_memory(**values)
        for plugin in self.plugins.semantic.values():
            try:
                plugin.index(record)
            except Exception as exc:
                self.plugin_errors.append(f"semantic:{plugin.name}: {exc}")
        for plugin in self.plugins.knowledge_graph.values():
            try:
                plugin.project(record)
            except Exception as exc:
                self.plugin_errors.append(f"knowledge_graph:{plugin.name}: {exc}")
        return record

    def search(self, **values: Any) -> list[RetrievalHit]:
        return self.retrieval.search(RetrievalContext(**values))

    def exact_source(self, memory_or_event_id: str) -> list[Event]:
        if memory_or_event_id.startswith("evt_"):
            return [self.store.get_event(memory_or_event_id)]
        if memory_or_event_id.startswith("view_"):
            view = self.store.get_tool_output_view_by_id(memory_or_event_id)
            return [self.store.get_event(view.event_id)]
        return self.store.provenance(memory_or_event_id)

    def project_source(self, relative_path: str, *, expected_hash: str | None = None) -> dict[str, Any]:
        return self.snapshots.read_project_source(relative_path, expected_hash=expected_hash)

    def create_snapshot(
        self,
        *,
        branch_id: str = "main",
        parent_snapshot_id: str | None = None,
        tracked_files: Sequence[str] | None = None,
    ) -> Snapshot:
        return self.snapshots.create(
            branch_id=branch_id,
            parent_snapshot_id=parent_snapshot_id,
            tracked_files=tracked_files,
        )

    def resume(
        self,
        *,
        branch_id: str = "main",
        token_budget: int = 1500,
        query: str = "resume current goal constraints decisions and open tasks",
    ) -> PromptPacket:
        budget = min(token_budget, 1500)
        snapshot = self.store.latest_snapshot(branch_id=branch_id)
        stale_reasons: tuple[str, ...] = ()
        snapshot_id: str | None = None
        if snapshot:
            restored = self.snapshots.restore(snapshot.id, branch_id=branch_id)
            stale_reasons = restored.stale_reasons
            snapshot_id = snapshot.id
            state = restored.state
        else:
            state = self.snapshots.build_state(branch_id=branch_id)
        started = time.perf_counter()
        packet = self.prompts.assemble(
            token_budget=budget,
            branch_id=branch_id,
            query=query,
            snapshot_id=snapshot_id,
            stale_reasons=stale_reasons,
            state=state,
            protected_instructions=(DURABLE_MEMORY_INSTRUCTION,),
        )
        self.usage.record_prompt(
            packet,
            branch_id=branch_id,
            operation="resume_packet",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return packet

    def capabilities(self, agent: str | None = None, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "core": {
                "append_only": True,
                "journal_mode": self.store.journal_mode,
                "snapshots": True,
                "snapshot_delta": "recursive-json-patch-v2",
                "lexical_retrieval": "sqlite-fts5-bm25" if self.store.fts_enabled else "python-fallback",
                "automatic_consolidation": "explicit-evidence-only",
                "durable_memory_markers": [
                    "Goal", "Decision", "Fact", "Constraint", "TODO", "Preference",
                ],
                "active_session_compaction": True,
                "tool_output_reduction": "canonical-raw-plus-command-aware-derived-views",
                "usage_observability": "provider-reported-plus-labelled-estimates",
                "budget_governor": "versioned-policy-with-snapshot-compact-handoff-actions",
                "task_boundaries": "task-branch-snapshot-resume",
                "code_context": {"python": "ast-symbol-call-impact", "other_languages": "unsupported"},
                "semantic_retrieval": bool(self.plugins.semantic),
                "knowledge_graph": bool(self.plugins.knowledge_graph),
                "kv_tiers": sorted(self.plugins.kv_tiers),
                "http_api": True,
                "mcp": True,
                "cli": True,
            },
            "plugin_errors": list(self.plugin_errors),
        }
        if agent:
            result["agent"] = adapter_capabilities(agent, supplied)
        else:
            result["adapters"] = {
                name: adapter_capabilities(name) for name in sorted(ADAPTERS)
            }
        return result

    def verify(self) -> dict[str, Any]:
        valid, error = self.store.verify_chain()
        return {
            "valid": valid,
            "error": error,
            "database_bytes": self.store.database_size(),
            "journal_mode": self.store.journal_mode,
        }
