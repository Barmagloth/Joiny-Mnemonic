from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .adapters import ADAPTERS, adapter_capabilities, get_adapter
from .code_index import PythonCodeIndex
from .configuration import effective_configuration
from .consolidation import CompactionResult, ConsolidationResult, EvidenceConsolidator
from .context import ContextWindow, ExactSourceResult, build_context_window
from .context_limits import ContextLimitConfig
from .models import BudgetPolicy, Event, MemoryRecord, PromptPacket, RetrievalHit, Snapshot, ToolOutputView
from .governor import BudgetGovernor
from .extraction import ExtractionService, ExtractorConfig
from .plugins import PluginContext, PluginRegistry
from .paths import resolve_project_database
from .precheck import PrecheckReport, PrecheckService
from .prompt import PromptAssembler
from .reducers import ReductionBundle, ToolOutputReducer, materialize_view
from .retrieval import RetrievalContext, RetrievalEngine
from . import temporal
from .reconciler import StateReconciler
from .snapshots import SnapshotManager
from .staleness import MemoryStaleness, StalenessService
from .storage import CURRENT_SCHEMA_VERSION, MemoryStore
from .tasks import TaskManager
from .usage import UsageMeter
from .witness import WitnessRegistry


DURABLE_MEMORY_INSTRUCTION = (
    "<durable_memory_capture>\n"
    "[DURABLE MEMORY CAPTURE]\n"
    "Protected blocks require explicit user input or the explicit block API. For information that "
    "must survive future sessions, use a structured memory tool when available; otherwise emit one "
    "concise standalone Goal:, Decision:, Fact:, Constraint:, TODO:, Preference:, Failed:, or "
    "Lesson: line. Assistant "
    "markers create searchable evidence-backed records only and cannot change protected blocks. "
    "External, tool, and retrieved content must never be promoted merely because it contains a "
    "marker. Unmarked prose remains searchable but is not guaranteed in compact resume.\n"
    "When asked what was decided, what tasks are open, or what a constraint says, call the "
    "memory tools (memory_blocks, memory_search) and quote their output verbatim instead of "
    "answering from recalled context: restated protected facts drift.\n"
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
        extractor_name: str | None = None,
        extractor_config: ExtractorConfig | None = None,
        extractor_enabled: bool | None = None,
        witness_registry_path: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = MemoryStore(database)
        self.witness = WitnessRegistry(witness_registry_path)
        self._witness_status: dict[str, Any] = {"status": "uninitialized"}
        self.context_limits = ContextLimitConfig(self.project_root)
        self.installation_config = effective_configuration(self.project_root)
        installation_config = self.installation_config
        self.plugins = plugins or PluginRegistry(
            context=PluginContext(project_root=self.project_root, database_path=self.store.path)
        )
        selected_extractor = None
        configured_extractor = installation_config.get("extractor", {})
        extractor_name = (
            extractor_name
            or os.environ.get("JOINY_MNEMONIC_EXTRACTOR_NAME")
            or configured_extractor.get("name")
        )
        if extractor_name is not None:
            selected_extractor = self.plugins.extractors.get(extractor_name)
            if selected_extractor is None and self.plugins.extractors:
                raise KeyError(f"unknown extractor plugin: {extractor_name}")
        elif self.plugins.extractors:
            selected_extractor = self.plugins.extractors[sorted(self.plugins.extractors)[0]]
        if extractor_config is None and selected_extractor is not None:
            extractor_config = ExtractorConfig(
                model_identity=str(
                    getattr(selected_extractor, "model_identity", selected_extractor.name)
                ),
                model_version=str(getattr(selected_extractor, "model_version", "unknown")),
                inference_parameters=dict(
                    getattr(selected_extractor, "inference_parameters", {})
                ),
            )
        active_policy = self.store.active_policy()
        policy_extraction_enabled = bool(
            active_policy
            and active_policy["policy"].get("automatic_extraction_enabled", False)
        )
        if (
            extractor_enabled is not None
            and bool(extractor_enabled) != policy_extraction_enabled
        ):
            self.store.close()
            raise ValueError(
                "extractor_enabled cannot override active policy; bootstrap or transition "
                "automatic_extraction_enabled in the policy ledger"
            )
        self.extraction = ExtractionService(
            self,
            selected_extractor,
            extractor_config,
            enabled=policy_extraction_enabled,
        )
        self.retrieval = RetrievalEngine(self.store, self.plugins)
        self.snapshots = SnapshotManager(self.store, self.project_root)
        self.staleness = StalenessService(self.store, self.project_root)
        self.prechecks = PrecheckService(self.store, self.staleness, self.project_root)
        self.usage = UsageMeter(self.store)
        self.prompts = PromptAssembler(
            self.store,
            self.retrieval,
            telemetry=self._record_prompt_injection,
        )
        self.consolidator = EvidenceConsolidator()
        self.code = PythonCodeIndex(self.project_root)
        self.reducer = ToolOutputReducer()
        self.tasks = TaskManager(self)
        self.governor = BudgetGovernor(self)
        self.reconciler = StateReconciler(self)
        self.plugin_errors = self.plugins.errors

    def _sync_extraction_policy(self) -> bool:
        active = self.store.active_policy()
        enabled = bool(
            active and active["policy"].get("automatic_extraction_enabled", False)
        )
        self.extraction.enabled = bool(
            enabled
            and self.extraction.extractor is not None
            and self.extraction.config is not None
        )
        return self.extraction.enabled

    def _repository_identity(self) -> str:
        try:
            remote = subprocess.run(
                ["git", "-C", str(self.project_root), "config", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
            initial = subprocess.run(
                ["git", "-C", str(self.project_root), "rev-list", "--max-parents=0", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return f"{remote}|{initial[0] if initial else ''}"

    def initialize_project(
        self,
        *,
        automatic_extraction_enabled: bool = False,
        automatic_task_closure_enabled: bool = False,
    ) -> dict[str, Any]:
        policy = {
            "version": 1,
            "automatic_extraction_enabled": bool(automatic_extraction_enabled),
            "automatic_task_closure_enabled": bool(automatic_task_closure_enabled),
            "auto_threshold": (
                self.extraction.config.auto_threshold
                if self.extraction.config is not None else 0.85
            ),
            "untrusted_evidence_zones": [
                "inline_code", "fenced_code", "blockquote"
            ],
            "auto_can_create_protected_blocks": False,
        }
        result = self.store.initialize_project(
            repository_identity=self._repository_identity(),
            canonical_path=str(self.project_root),
            code_version="0.8.0",
            policy=policy,
        )
        self._sync_extraction_policy()
        if result.get("initialized"):
            self._witness_status = self.witness.check_and_update(
                self.store, allow_first=True
            )
        else:
            self._witness_status = self.security_status()["witness"]
        return {**result, "witness": self._witness_status}

    def security_status(self) -> dict[str, Any]:
        witness = self.witness.check_and_update(self.store)
        finding_type = witness.get("finding")
        if finding_type:
            details = dict(witness.get("details", {}))
            digest = hashlib.sha256(
                json.dumps(
                    details, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            ).hexdigest()[:24]
            self.store.record_security_finding(
                str(finding_type),
                incident_key=f"{finding_type}:{digest}",
                details=details,
            )
        self._witness_status = witness
        findings = self.store.list_security_findings()
        return {
            "witness": witness,
            "findings": findings,
            "active_security_findings": sum(
                item["status"] != "acknowledged" for item in findings
            ),
            "acknowledged_security_findings": sum(
                item["status"] == "acknowledged" for item in findings
            ),
        }

    def request_finding_acknowledgement(
        self,
        finding_id: str,
        *,
        branch_id: str = "main",
        origin_evidence_type: str = "extractor",
    ) -> dict[str, str]:
        event, transition = self.store.append_finding_ack_request(
            finding_id,
            branch_id=branch_id,
            origin_evidence_type=origin_evidence_type,
        )
        self.checkpoint_witness()
        return {"event_id": event.id, "transition_id": transition}

    def acknowledge_finding_from_user(
        self, finding_id: str, *, source_event_id: str
    ) -> str:
        return self.store.transition_finding(
            finding_id,
            "acknowledged",
            source_event_id=source_event_id,
            actor="logical_user",
            origin_evidence_type="host_logical_user",
        )

    def request_policy_change(
        self, policy: dict[str, Any], *, branch_id: str = "main"
    ) -> Event:
        event = self.store.append_event(
            kind="state",
            role=None,
            branch_id=branch_id,
            content="policy change requested",
            payload={
                "operation": "policy_change_requested",
                "policy": policy,
                "active_policy_id": (
                    self.store.active_policy() or {}
                ).get("id"),
            },
        )
        self.checkpoint_witness()
        return event
    def budget_policy(
        self, *, branch_id: str = "main", agent: str | None = None
    ) -> BudgetPolicy:
        if agent:
            configured = self.context_limits.resolve(agent, branch_id=branch_id)
            if configured is not None:
                return configured
        return self.store.get_budget_policy(branch_id=branch_id)

    def close(self) -> None:
        self.extraction.close()
        closed: set[int] = set()
        for collection in (
            self.plugins.semantic,
            self.plugins.knowledge_graph,
            self.plugins.kv_tiers,
            self.plugins.extractors,
        ):
            for plugin in collection.values():
                if id(plugin) in closed:
                    continue
                close = getattr(plugin, "close", None)
                if callable(close):
                    close()
                closed.add(id(plugin))
        self.store.close()

    def __enter__(self) -> MemoryService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def checkpoint_witness(self) -> dict[str, Any]:
        self._witness_status = self.witness.check_and_update(self.store)
        return self._witness_status
    def append_event(self, **values: Any) -> Event:
        for untrusted_key in ("origin_channel", "origin_adapter", "origin_evidence_type"):
            values.pop(untrusted_key, None)
        event = self.store.append_event(**values)
        self.consolidator.consolidate_event(self, event)
        self.extraction.notify()
        self.checkpoint_witness()
        return event

    def request_candidate_transition(
        self,
        candidate_id: str,
        action: str,
        *,
        branch_id: str = "main",
        replacement_candidate_id: str | None = None,
        replacement_memory_id: str | None = None,
        origin_evidence_type: str = "extractor",
    ) -> dict[str, str]:
        mapping = {
            "confirm": "confirmation_requested",
            "reject": "rejection_requested",
            "supersede": "supersession_requested",
        }
        if action not in mapping:
            raise ValueError("action must be confirm, reject or supersede")
        event, transition_id = self.store.append_candidate_request(
            candidate_id,
            mapping[action],
            branch_id=branch_id,
            action=action,
            origin_evidence_type=origin_evidence_type,
            replacement_candidate_id=replacement_candidate_id,
            replacement_memory_id=replacement_memory_id,
        )
        self.checkpoint_witness()
        return {"event_id": event.id, "transition_id": transition_id}

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
        self.extraction.notify()
        self.checkpoint_witness()
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
        values.setdefault("metadata", {"origin": "explicit", "authority_level": "confirmed"})
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
        include_staleness = bool(values.pop("include_staleness", False))
        record_telemetry = bool(values.pop("record_telemetry", True))
        session_id = values.pop("session_id", None)
        task_key = values.pop("task_key", None)
        telemetry_receipt = values.pop("telemetry_receipt", None)
        context = RetrievalContext(**values)
        hits = self.retrieval.search(context)
        if record_telemetry:
            try:
                self.usage.record_retrieval_search(
                    branch_id=context.branch_id,
                    session_id=session_id,
                    task_key=task_key,
                    query=context.query,
                    hits=hits,
                    semantic_enabled=bool(context.semantic and self.plugins.semantic),
                    filters={
                        "memory_types": context.memory_types,
                        "file": context.file,
                        "since": context.since,
                        "until": context.until,
                        "exact": context.exact,
                        "include_events": context.include_events,
                        **(
                            {
                                "valid_at": context.valid_at,
                                "known_at": context.known_at,
                                "current": context.current,
                                "include_unknown_validity": (
                                    context.include_unknown_validity
                                ),
                                "history": context.history,
                            }
                            if context.temporal_active
                            else {}
                        ),
                    },
                    limit=context.limit,
                    receipt_key=telemetry_receipt,
                )
            except Exception:
                pass
        if not include_staleness:
            return hits
        inspections = {
            item.memory_id: item
            for item in self.staleness.inspect(
                branch_id=context.branch_id,
                memory_ids=tuple(
                    hit.id for hit in hits if hit.source_kind == "memory"
                ),
            )
        }
        return [
            replace(
                hit,
                metadata={
                    **hit.metadata,
                    **(
                        {"staleness": asdict(inspections[hit.id])}
                        if hit.id in inspections
                        else {}
                    ),
                },
            )
            for hit in hits
        ]

    def _record_prompt_injection(
        self, packet: PromptPacket, context: dict[str, Any]
    ) -> None:
        self.usage.record_prompt_injection(
            packet,
            branch_id=str(context["branch_id"]),
            session_id=context.get("session_id"),
            task_key=context.get("task_key"),
            query=str(context.get("query", "")),
            latency_ms=context.get("latency_ms"),
            receipt_key=context.get("receipt_key"),
        )

    def stale(self, **values: Any) -> tuple[MemoryStaleness, ...]:
        return self.staleness.inspect(**values)

    def precheck(self, **values: Any) -> PrecheckReport:
        return self.prechecks.run(**values)

    def knowledge_neighbors(
        self, entity: str, *, branch_id: str = "main", limit: int = 20
    ) -> list[RetrievalHit]:
        if limit < 1:
            return []
        records = self.store.list_memories(branch_id=branch_id)
        filters = {
            "branch_id": branch_id,
            "allowed_memory_ids": tuple(record.id for record in records),
        }
        hits: list[RetrievalHit] = []
        for plugin in self.plugins.knowledge_graph.values():
            try:
                sync = getattr(plugin, "sync", None)
                if callable(sync):
                    sync(records)
                else:
                    for record in records:
                        plugin.project(record)
                hits.extend(plugin.neighbors(entity, limit=limit, filters=filters))
            except Exception as exc:
                error = f"knowledge_graph:{plugin.name}: {exc}"
                if error not in self.plugin_errors:
                    self.plugin_errors.append(error)
        deduplicated: dict[str, RetrievalHit] = {}
        for hit in hits:
            current = deduplicated.get(hit.id)
            if current is None or hit.score > current.score:
                deduplicated[hit.id] = hit
        return sorted(
            deduplicated.values(),
            key=lambda hit: (hit.score, hit.created_at),
            reverse=True,
        )[:limit]

    def _resolve_exact_source(self, identifier: str) -> tuple[tuple[Event, ...], str]:
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("source identifier must be a non-empty string")
        if identifier.startswith("evt_"):
            event = self.store.get_event(identifier)
            return (event,), event.branch_id
        if identifier.startswith("view_"):
            view = self.store.get_tool_output_view_by_id(identifier)
            event = self.store.get_event(view.event_id)
            return (event,), event.branch_id
        if identifier.startswith("replay:evt_"):
            derivation = self.store.get_event(identifier.removeprefix("replay:"))
            source_ids = tuple(derivation.payload.get("source_event_ids", ()))
            if source_ids:
                return (
                    tuple(self.store.get_event(event_id) for event_id in source_ids),
                    derivation.branch_id,
                )
            return (derivation,), derivation.branch_id
        if identifier.startswith("mem_"):
            record = self.store.get_memory(identifier)
            return tuple(self.store.provenance(identifier)), record.branch_id

        for plugin in self.plugins.knowledge_graph.values():
            resolver = getattr(plugin, "resolve_source_ids", None)
            if not callable(resolver):
                continue
            try:
                source_ids = tuple(resolver(identifier) or ())
            except Exception as exc:
                error = f"knowledge_graph:{plugin.name}: {exc}"
                if error not in self.plugin_errors:
                    self.plugin_errors.append(error)
                continue
            if source_ids:
                events = tuple(self.store.get_event(event_id) for event_id in source_ids)
                source_branch = events[0].branch_id
                branch_resolver = getattr(plugin, "resolve_branch_id", None)
                if callable(branch_resolver):
                    try:
                        source_branch = str(branch_resolver(identifier) or source_branch)
                    except Exception as exc:
                        error = f"knowledge_graph:{plugin.name}: {exc}"
                        if error not in self.plugin_errors:
                            self.plugin_errors.append(error)
                return events, source_branch
        raise KeyError(f"unknown source identifier: {identifier}")

    def _record_promotion(self, identifier: str, events: Sequence[Event]) -> None:
        """Best-effort D5 telemetry; promotion must never fail over it."""
        try:
            family = "memory" if identifier.startswith("mem_") else "event"
            for event in events:
                if event.kind == "tool_output":
                    from .reducers import ToolOutputReducer

                    family = ToolOutputReducer.family(event)
                    break
            branch = events[0].branch_id if events else "main"
            self.usage.record_source_promotion(
                branch_id=branch, target_id=identifier, family=family
            )
        except Exception:
            pass

    def exact_source(self, memory_or_event_id: str) -> list[Event]:
        events, _ = self._resolve_exact_source(memory_or_event_id)
        self._record_promotion(memory_or_event_id, events)
        return list(events)

    def exact_sources(self, ids: Sequence[str]) -> tuple[ExactSourceResult, ...]:
        if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
            raise TypeError("ids must be a sequence of source identifiers")
        identifiers = tuple(dict.fromkeys(str(identifier) for identifier in ids))
        if not identifiers:
            raise ValueError("at least one source identifier is required")
        results: list[ExactSourceResult] = []
        for identifier in identifiers:
            events, _ = self._resolve_exact_source(identifier)
            self._record_promotion(identifier, events)
            results.append(
                ExactSourceResult(
                    id=identifier,
                    source_event_ids=tuple(event.id for event in events),
                    events=events,
                )
            )
        return tuple(results)

    def context_around(
        self,
        id: str,
        *,
        before: int = 3,
        after: int = 3,
        include_source: bool = False,
        branch_id: str | None = None,
    ) -> ContextWindow:
        source_events, source_branch = self._resolve_exact_source(id)
        return build_context_window(
            self.store,
            id,
            source_events,
            branch_id=branch_id or source_branch,
            before=before,
            after=after,
            include_source=include_source,
        )

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

    def prune_snapshots(
        self,
        snapshot_ids: Sequence[str],
        *,
        branch_id: str = "main",
    ) -> dict[str, Any]:
        result = self.store.prune_snapshot_blobs(snapshot_ids, branch_id=branch_id)
        if result["event_id"] is not None:
            self.checkpoint_witness()
        return result

    def resume(
        self,
        *,
        branch_id: str = "main",
        token_budget: int = 1500,
        query: str = "resume current goal constraints decisions and open tasks",
        session_id: str | None = None,
        task_key: str | None = None,
        telemetry_receipt: str | None = None,
        record_telemetry: bool = True,
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
        security = self.security_status()
        active_findings = [
            item for item in security["findings"]
            if item["status"] != "acknowledged"
        ]
        if active_findings:
            stale_reasons = (
                *stale_reasons,
                *(
                    "security finding "
                    f"{item['finding_type']} id={item['id']} source={item['source_event_id']}"
                    for item in active_findings
                ),
            )
        extraction_status = self.extraction.status()
        if extraction_status.oldest_pending_age is not None:
            stale_reasons = (
                *stale_reasons,
                "automatic extraction backlog is incomplete; "
                f"oldest_pending_age={extraction_status.oldest_pending_age:.1f}s",
            )
        packet = self.prompts.assemble(
            token_budget=budget,
            branch_id=branch_id,
            query=query,
            snapshot_id=snapshot_id,
            stale_reasons=stale_reasons,
            state=state,
            protected_instructions=(DURABLE_MEMORY_INSTRUCTION,),
            session_id=session_id,
            task_key=task_key,
            telemetry_receipt=telemetry_receipt,
            record_telemetry=record_telemetry,
        )
        return packet

    def _agent_capabilities(
        self, agent: str, supplied: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], list[str]]:
        from .hooks import hook_installation_status

        values = adapter_capabilities(agent, supplied)
        warnings: list[str] = []
        try:
            installation = hook_installation_status(self.project_root, agent)
        except ValueError:
            installation = {
                "status": "unsupported",
                "configured": False,
                "config_valid": True,
                "checked_paths": [],
                "configured_paths": [],
                "configured_scopes": [],
                "invalid_configs": [],
                "install_command": None,
            }
        runtime_verified = self.store.has_hook_activity(agent)
        if supplied is None:
            effective = installation["configured"] and runtime_verified
            for key in (
                "event_ingestion",
                "automatic_resume",
                "tool_capture",
                "tool_failure_capture",
                "pre_action_precheck",
                "active_compaction",
            ):
                values[key] = bool(values[key] and effective)
        values.update(
            {
                "hook_installer_available": values["hook_installer"],
                "hooks_configured": installation["configured"],
                "hook_configuration_status": installation["status"],
                "hook_config_valid": installation["config_valid"],
                "hook_checked_paths": installation["checked_paths"],
                "hook_configured_paths": installation["configured_paths"],
                "hook_configured_scopes": installation["configured_scopes"],
                "hook_invalid_configs": installation["invalid_configs"],
                "hook_install_command": installation["install_command"],
                "hook_runtime_verified": runtime_verified,
            }
        )
        expected_database = resolve_project_database(self.project_root).resolve()
        active_database = self.store.path
        database_matches = (
            None if str(active_database) == ":memory:"
            else active_database.resolve() == expected_database
        )
        values.update(
            {
                "hook_expected_database_path": str(expected_database),
                "active_database_path": str(active_database),
                "hook_database_matches": database_matches,
            }
        )
        if installation["invalid_configs"]:
            paths = ", ".join(
                item["path"] for item in installation["invalid_configs"]
            )
            warnings.append(
                f"{agent} hook configuration contains invalid JSON or is unreadable: {paths}"
            )
        if not installation["configured"] and installation["status"] != "unsupported":
            warnings.append(
                f"{agent} automatic capture is not configured; MCP alone does not "
                "capture ordinary conversation text or durable marker lines"
            )
        elif installation["configured"] and not runtime_verified:
            warnings.append(
                f"{agent} hook configuration was detected, but this database has "
                "not observed a hook delivery yet"
            )
        if installation["configured"] and database_matches is False:
            warnings.append(
                f"{agent} hooks target {expected_database}, but this process opened "
                f"{active_database}; automatic capture and MCP search are split"
            )
        return values, warnings

    def capabilities(
        self, agent: str | None = None, supplied: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "core": {
                "append_only": True,
                "journal_mode": self.store.journal_mode,
                "snapshots": True,
                "snapshot_format": "full-zlib-v1",
                "legacy_snapshot_reader": "json-patch-v2",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "schema_migrations": "versioned-backup-before-migrate-future-version-fail-closed",
                "lexical_retrieval": "sqlite-fts5-bm25" if self.store.fts_enabled else "python-fallback",
                "automatic_consolidation": "explicit-evidence-only",
                "durable_memory_markers": [
                    "Goal", "Decision", "Fact", "Constraint", "TODO", "Preference",
                    "Failed", "Failure", "Lesson",
                ],
                "active_session_compaction": True,
                "precheck": "deterministic-warning-only",
                "tool_output_reduction": "canonical-raw-plus-command-aware-derived-views",
                "usage_observability": "provider-reported-plus-labelled-estimates",
                "budget_governor": "versioned-policy-with-snapshot-compact-handoff-actions",
                "task_boundaries": "task-branch-snapshot-resume",
                "code_context": {"python": "ast-symbol-call-impact", "other_languages": "unsupported"},
                "semantic_retrieval": bool(self.plugins.semantic),
                "knowledge_graph": bool(self.plugins.knowledge_graph),
                "kv_tiers": sorted(self.plugins.kv_tiers),
                "extractor_plugin_category": True,
                "http_api": True,
                "mcp": True,
                "cli": True,
                "state_maintenance": {
                    "automatic_task_closure_enabled": (
                        bool(
                            (self.store.active_policy() or {"policy": {}})["policy"].get(
                                "automatic_task_closure_enabled", False
                            )
                        )
                    ),
                    "pending_task_completions": self.reconciler.pending_completions(),
                    "hygiene_findings": self.reconciler.hygiene_findings(),
                },
                "bitemporal_retrieval": {
                    "valid_time_fields": True,
                    "controls": (
                        "valid_at", "known_at", "current",
                        "include_unknown_validity", "history",
                    ),
                    "temporal_projection_code_version": (
                        temporal.TEMPORAL_PROJECTION_CODE_VERSION
                    ),
                },
            },
            "setup_configuration": self.installation_config,
            "plugin_errors": list(self.plugin_errors),
            "warnings": [],
        }
        result.update(asdict(self.extraction.status()))
        security = self.security_status()
        result.update({
            "active_security_findings": security["active_security_findings"],
            "acknowledged_security_findings": security["acknowledged_security_findings"],
            "witness_status": security["witness"]["status"],
            "security_findings": security["findings"],
        })
        if agent:
            agent_values, warnings = self._agent_capabilities(agent, supplied)
            result["agent"] = agent_values
            result["warnings"].extend(warnings)
        else:
            adapters: dict[str, Any] = {}
            for name in sorted(ADAPTERS):
                values, warnings = self._agent_capabilities(name)
                adapters[name] = values
                result["warnings"].extend(warnings)
            result["adapters"] = adapters
        return result

    def verify(self) -> dict[str, Any]:
        valid, error = self.store.verify_chain()
        return {
            "valid": valid,
            "error": error,
            "database_bytes": self.store.database_size(),
            "journal_mode": self.store.journal_mode,
        }
