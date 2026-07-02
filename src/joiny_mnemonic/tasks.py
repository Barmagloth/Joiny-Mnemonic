from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

from .models import PromptPacket, TaskRecord

if TYPE_CHECKING:
    from .service import MemoryService


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

    def set_status(
        self,
        task_key: str,
        status: str,
        *,
        note: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        current = self.service.store.get_task(task_key)
        source = self.service.store.append_event(
            branch_id=current.branch_id,
            session_id=session_id,
            kind="state",
            role=None,
            content=f"Task {status}: {current.title}" + (f"\n{note}" if note else ""),
            payload={"task": {"key": task_key, "status": status, "note": note}},
        )
        snapshot = self.service.create_snapshot(branch_id=current.branch_id)
        task = self.service.store.create_task_version(
            task_key=task_key,
            branch_id=current.branch_id,
            title=current.title,
            status=status,
            parent_task_key=current.parent_task_key,
            source_event_ids=[*current.source_event_ids, source.id],
            snapshot_id=snapshot.id,
            metadata={**current.metadata, **(metadata or {})},
        )
        if session_id is not None:
            self.service.store.bind_task_session(session_id, task_key)
        return task

    def complete(
        self,
        task_key: str,
        *,
        note: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        return self.set_status(
            task_key,
            "completed",
            note=note,
            session_id=session_id,
            metadata=metadata,
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
        )

    def list(self, *, status: str | None = None) -> tuple[TaskRecord, ...]:
        return self.service.store.list_tasks(status=status)