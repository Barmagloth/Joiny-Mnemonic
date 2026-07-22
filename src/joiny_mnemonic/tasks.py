from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from functools import wraps
from typing import TYPE_CHECKING, Any

from .models import PromptPacket, TaskRecord
from .provenance import WORKSTREAM_REQUEST_OPERATION

if TYPE_CHECKING:
    from .service import MemoryService


def _atomic(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(self: "TaskManager", *args: Any, **kwargs: Any) -> Any:
        with self.service.store._transaction():
            return method(self, *args, **kwargs)

    return wrapped


class TaskManager:
    """Task boundaries mapped to immutable branch lineage and atomic snapshots."""

    def __init__(self, service: MemoryService) -> None:
        self.service = service

    @staticmethod
    def branch_name(task_key: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_key).strip("-.").lower()[:48]
        slug = slug or "task"
        digest = hashlib.sha256(task_key.encode("utf-8")).hexdigest()[:8]
        return f"task/{slug}-{digest}"

    @_atomic
    def start(
        self,
        task_key: str,
        title: str,
        *,
        parent_branch: str = "main",
        parent_task_key: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        try:
            existing = self.service.store.get_task(task_key)
        except KeyError:
            existing = None
        if existing is not None:
            if existing.status not in {"active", "blocked"}:
                raise ValueError(f"task {task_key!r} is already {existing.status}")
            if session_id is not None:
                self.service.store.bind_task_session(session_id, task_key)
            return existing
        if parent_task_key is not None:
            parent_branch = self.service.store.get_task(parent_task_key).branch_id
        visible = self.service.store.query_events(branch_id=parent_branch)
        fork_seq = visible[-1].seq if visible else None
        branch_id = self.branch_name(task_key)
        self.service.store.create_branch(
            branch_id, parent_id=parent_branch, fork_event_seq=fork_seq
        )
        source = self.service.store.append_event(
            branch_id=branch_id,
            session_id=session_id,
            kind="state",
            role=None,
            content=f"Task started: {title}",
            payload={
                "task": {
                    "key": task_key,
                    "title": title,
                    "status": "active",
                    "parent_task_key": parent_task_key,
                }
            },
        )
        self.service.store.set_active_block(
            "goal",
            title,
            branch_id=branch_id,
            session_id=session_id,
            source_event_ids=[source.id],
        )
        snapshot = self.service.create_snapshot(branch_id=branch_id)
        task = self.service.store.create_task_version(
            task_key=task_key,
            branch_id=branch_id,
            title=title,
            status="active",
            parent_task_key=parent_task_key,
            source_event_ids=[source.id],
            snapshot_id=snapshot.id,
            metadata=metadata,
        )
        if session_id is not None:
            self.service.store.bind_task_session(session_id, task_key)
        return task

    def obligations(self, task_key: str) -> tuple[dict[str, str], ...]:
        task = self.service.store.get_task(task_key)
        obligations: list[dict[str, str]] = []
        block = self.service.store.get_active_blocks(
            branch_id=task.branch_id
        ).get("open_tasks")
        if block is not None:
            for index, raw in enumerate(block.content.splitlines()):
                entry = raw.strip().removeprefix("- ").strip()
                if entry:
                    obligations.append({
                        "id": f"open_tasks:{block.id}:{index}",
                        "kind": "open_tasks",
                        "content": entry,
                    })
        lineage = dict(self.service.store.branch_lineage(task.branch_id))
        for candidate in self.service.store.list_settlement_candidates(
            kind="task_closure"
        ):
            if str(candidate.get("status") or "pending") == "applied":
                continue
            source = self.service.store.get_event(str(candidate["source_event_id"]))
            if source.branch_id not in lineage:
                continue
            cutoff = lineage[source.branch_id]
            if cutoff is not None and source.seq > int(cutoff):
                continue
            obligations.append({
                "id": str(candidate["id"]),
                "kind": "task_closure",
                "content": str(candidate["normalized_content"]),
            })
        return tuple(obligations)

    def ensure(
        self,
        task_key: str,
        *,
        title: str | None = None,
        parent_branch: str = "main",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        try:
            task = self.service.store.get_task(task_key)
        except KeyError:
            return self.start(
                task_key,
                title or task_key,
                parent_branch=parent_branch,
                session_id=session_id,
                metadata=metadata,
            )
        if session_id is not None:
            self.service.store.bind_task_session(session_id, task_key)
        return task

    @_atomic
    def set_status(
        self,
        task_key: str,
        status: str,
        *,
        note: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        source_event_id: str | None = None,
        override_obligations: Sequence[str] | None = None,
        override_reason: str = "",
        _reopen: bool = False,
        _reason: str = "",
    ) -> TaskRecord:
        current = self.service.store.get_task(task_key)
        if current.status == status:
            return current
        supplied_overrides = tuple(dict.fromkeys(override_obligations or ()))
        if status == "completed":
            obligations = self.obligations(task_key)
            required = {str(item["id"]) for item in obligations}
            supplied = set(supplied_overrides)
            if required:
                if not override_reason.strip():
                    raise PermissionError(
                        "workstream has open obligations; override_reason is required "
                        f"with exact ids: {sorted(required)}"
                    )
                if supplied != required or len(supplied_overrides) != len(required):
                    raise PermissionError(
                        "override_obligations must exactly match current obligations: "
                        f"{sorted(required)}"
                    )
            elif supplied_overrides:
                raise ValueError("override_obligations contains stale or unknown ids")
        elif supplied_overrides or override_reason:
            raise ValueError("obligation override is valid only for completion")
        if status in {"completed", "cancelled"} and source_event_id is None:
            raise PermissionError(
                f"task {status} requires a trusted source_event_id"
            )
        if source_event_id is None:
            source = self.service.store.append_event(
                branch_id=current.branch_id,
                session_id=session_id,
                kind="state",
                role=None,
                content=f"Task {status}: {current.title}" + (f"\n{note}" if note else ""),
                payload={"task": {"key": task_key, "status": status, "note": note}},
            )
            source_event_id = source.id
        else:
            source = self.service.store.get_event(source_event_id)
        transition_payload: dict[str, Any] = {
            "operation": "workstream_status_changed",
            "task_key": task_key,
            "from_status": current.status,
            "to_status": status,
            "evidence_event_id": source.id,
            "note": note,
        }
        if supplied_overrides:
            transition_payload["override_obligations"] = list(supplied_overrides)
            transition_payload["override_reason"] = override_reason.strip()
        transition_events, _ = self.service.store.append_internal_events_once(
            f"workstream-status:{task_key}:{current.version}:{status}",
            [{
                "kind": "state",
                "role": None,
                "content": f"workstream status changed: {task_key} {current.status} -> {status}",
                "payload": transition_payload,
            }],
            branch_id=current.branch_id,
            session_id=session_id,
        )
        snapshot = self.service.create_snapshot(branch_id=current.branch_id)
        task = self.service.store.create_task_version(
            task_key=task_key,
            branch_id=current.branch_id,
            title=current.title,
            status=status,
            parent_task_key=current.parent_task_key,
            source_event_ids=[
                *current.source_event_ids,
                transition_events[0].id,
                source.id,
            ],
            snapshot_id=snapshot.id,
            metadata={**current.metadata, **(metadata or {})},
            reopen=_reopen,
            transition_reason=_reason,
        )
        if session_id is not None:
            self.service.store.bind_task_session(session_id, task_key)
        return task

    @_atomic
    def set_status_as_operator(
        self,
        task_key: str,
        status: str,
        *,
        note: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        override_obligations: Sequence[str] | None = None,
        override_reason: str = "",
    ) -> TaskRecord:
        """Apply one local-operator transition with process-authored evidence."""
        current = self.service.store.get_task(task_key)
        if current.status == status:
            return current
        if status == "active" and current.status in {"completed", "cancelled"}:
            raise ValueError("use task-reopen for a terminal workstream")
        events, _ = self.service.store.append_internal_events_once(
            f"workstream-request:{task_key}:{current.version}:{status}:operator",
            [{
                "kind": "state",
                "role": None,
                "content": f"workstream {status} requested by local operator: {task_key}",
                "payload": {
                    "operation": WORKSTREAM_REQUEST_OPERATION,
                    "task_key": task_key,
                    "transition": status,
                    "reason": note,
                    "requested_by": "operator",
                },
            }],
            branch_id=current.branch_id,
            session_id=session_id,
        )
        return self.set_status(
            task_key,
            status,
            note=note,
            session_id=session_id,
            metadata=metadata,
            source_event_id=events[0].id,
            override_obligations=override_obligations,
            override_reason=override_reason,
        )

    @_atomic
    def reopen_as_operator(
        self,
        task_key: str,
        *,
        reason: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("workstream reopen requires a non-empty reason")
        current = self.service.store.get_task(task_key)
        if current.status not in {"completed", "cancelled"}:
            raise ValueError("only a terminal workstream can be reopened")
        events, _ = self.service.store.append_internal_events_once(
            f"workstream-reopen:{task_key}:{current.version}:operator",
            [{
                "kind": "state",
                "role": None,
                "content": f"workstream reopen requested by local operator: {task_key}",
                "payload": {
                    "operation": WORKSTREAM_REQUEST_OPERATION,
                    "task_key": task_key,
                    "transition": "active",
                    "reason": reason,
                    "requested_by": "operator",
                },
            }],
            branch_id=current.branch_id,
            session_id=session_id,
        )
        return self.reopen(
            task_key,
            reason=reason,
            source_event_id=events[0].id,
            session_id=session_id,
            metadata=metadata,
        )

    def complete(
        self,
        task_key: str,
        *,
        note: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        source_event_id: str | None = None,
        override_obligations: Sequence[str] | None = None,
        override_reason: str = "",
    ) -> TaskRecord:
        return self.set_status(
            task_key,
            "completed",
            note=note,
            session_id=session_id,
            metadata=metadata,
            source_event_id=source_event_id,
            override_obligations=override_obligations,
            override_reason=override_reason,
        )

    def reopen(
        self,
        task_key: str,
        *,
        reason: str,
        source_event_id: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        return self.set_status(
            task_key,
            "active",
            note=reason,
            session_id=session_id,
            metadata=metadata,
            source_event_id=source_event_id,
            _reopen=True,
            _reason=reason,
        )

    def resume(
        self,
        task_key: str,
        *,
        token_budget: int = 1500,
        query: str | None = None,
    ) -> PromptPacket:
        task = self.service.store.get_task(task_key)
        return self.service.resume(
            branch_id=task.branch_id,
            token_budget=token_budget,
            query=query or f"resume task {task.task_key}: {task.title}",
            task_key=task.task_key,
        )

    def list(self, *, status: str | None = None) -> tuple[TaskRecord, ...]:
        return self.service.store.list_tasks(status=status)
