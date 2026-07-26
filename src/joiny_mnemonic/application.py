from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from .models import ActiveBlock, Artifact, BudgetPolicy

if TYPE_CHECKING:
    from .service import MemoryService


class ApplicationCommands:
    """Mutation boundary shared by CLI, MCP, HTTP, and host hooks."""

    def __init__(self, service: MemoryService) -> None:
        self._service = service

    def record_security_finding(
        self, finding_type: str, *, incident_key: str, details: dict[str, Any]
    ) -> str:
        return self._service.store.record_security_finding(
            finding_type, incident_key=incident_key, details=details
        )

    def start_session(
        self,
        agent: str,
        *,
        branch_id: str = "main",
        capabilities: dict[str, Any] | None = None,
    ) -> str:
        return self._service.store.start_session(
            agent, branch_id=branch_id, capabilities=capabilities
        )

    def create_branch(
        self,
        branch_id: str,
        *,
        parent_id: str = "main",
        fork_event_seq: int | None = None,
    ) -> str:
        return self._service.store.create_branch(
            branch_id, parent_id=parent_id, fork_event_seq=fork_event_seq
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
        return self._service.store.append_artifact(
            name=name,
            data=data,
            mime_type=mime_type,
            branch_id=branch_id,
            session_id=session_id,
            files=files,
        )

    def set_active_block(
        self,
        name: str,
        content: str,
        *,
        branch_id: str = "main",
        session_id: str | None = None,
        source_event_ids: Sequence[str] = (),
    ) -> ActiveBlock:
        return self._service.store.set_active_block(
            name,
            content,
            branch_id=branch_id,
            session_id=session_id,
            source_event_ids=source_event_ids,
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
        return self._service.store.set_budget_policy(
            branch_id=branch_id,
            context_window_tokens=context_window_tokens,
            snapshot_ratio=snapshot_ratio,
            compact_ratio=compact_ratio,
            handoff_ratio=handoff_ratio,
            hard_limit_ratio=hard_limit_ratio,
            min_action_interval_events=min_action_interval_events,
        )
