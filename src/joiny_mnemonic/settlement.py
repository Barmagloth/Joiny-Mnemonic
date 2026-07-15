"""Settlement surfaces (task6.md 6C).

The candidate ledger (6B) is the settlement machine; this module is its
manual face: `candidates show/settle` on the CLI and the MCP read/write
tools. Manual settlement is the exception, not the routine — the default
path stays autonomous (reconciler) — and every manual verb is
trust-hardened: the ledger transition cites one canonical internal
`settlement_requested` event whose derived origin (local_operator or
delegated_agent) no public-API or MCP text can mint. Untrusted text can
request; it can never settle (H1 discipline). A delegated agent
additionally needs the policy ledger to enable
`agent_settlement_delegation_enabled`. Enforcement stays `recorded_only`:
settlement is audit evidence, never OS authority.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .consolidation import EvidenceConsolidator
from .provenance import SETTLEMENT_REQUEST_OPERATION

if TYPE_CHECKING:
    from .models import Event
    from .service import MemoryService


MANUAL_TRANSITIONS = ("applied", "contested", "reverted")
_REQUESTORS = {"operator", "agent"}
_KINDS_WITH_SEMANTICS = {"task_closure", "block_change"}
_REPLACE_BLOCKS = {"goal", "instructions"}


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


class SettlementSurface:
    """Manual show/settle verbs over the candidate ledger."""

    def __init__(self, service: "MemoryService") -> None:
        self.service = service
        self.store = service.store

    def show(self, candidate_id: str) -> dict[str, Any]:
        return self.store.get_settlement_candidate(candidate_id)

    def agent_delegation_enabled(self) -> bool:
        active = self.store.active_policy()
        return bool(
            active
            and active["policy"].get("agent_settlement_delegation_enabled", False)
        )

    def settle(
        self,
        candidate_id: str,
        transition: str,
        *,
        reason: str,
        requested_by: str,
        branch_id: str = "main",
    ) -> dict[str, Any]:
        if transition not in MANUAL_TRANSITIONS:
            raise ValueError(f"unsupported manual transition: {transition}")
        if requested_by not in _REQUESTORS:
            raise ValueError("requested_by must be operator or agent")
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("manual settlement requires a non-empty reason")
        candidate = self.store.get_settlement_candidate(candidate_id)
        kind = str(candidate["candidate_kind"])
        if kind not in _KINDS_WITH_SEMANTICS:
            raise ValueError(f"no manual settlement semantics for kind: {kind}")
        current = str(candidate["status"] or "pending")
        summary = {
            "candidate_id": candidate_id,
            "kind": kind,
            "transition": transition,
            "requested_by": requested_by,
        }
        if current == transition:
            return {**summary, "already_settled": True}
        # Fail closed BEFORE any side effect, mirroring the ledger rule.
        if transition not in self.store._SETTLEMENT_FLOW.get(current, set()):
            raise ValueError(
                f"conflicting settlement: {current} -> {transition} is not allowed"
            )
        if requested_by == "agent" and not self.agent_delegation_enabled():
            raise PermissionError(
                "agent settlement requires explicit policy delegation "
                "(agent_settlement_delegation_enabled); ask the user to run "
                "`joiny-mnemonic candidates settle` instead"
            )
        request_events, _ = self.store.append_internal_events_once(
            f"settlement-request:{candidate_id}:{transition}:{requested_by}",
            [
                {
                    "kind": "state",
                    "role": None,
                    "content": (
                        f"settlement {transition} requested by {requested_by}: "
                        f"{candidate_id}"
                    ),
                    "payload": {
                        "operation": SETTLEMENT_REQUEST_OPERATION,
                        "candidate_id": candidate_id,
                        "candidate_kind": kind,
                        "transition": transition,
                        "reason": reason,
                        "requested_by": requested_by,
                        "enforcement_level": "recorded_only",
                    },
                }
            ],
            branch_id=branch_id,
        )
        request_event = request_events[0]
        rule_id = f"manual_settle_{requested_by}"
        if kind == "task_closure":
            result = self._settle_task_closure(
                candidate, transition, request_event=request_event,
                actor=requested_by, rule_id=rule_id, reason=reason,
                branch_id=branch_id,
            )
        else:
            result = self._settle_block_change(
                candidate, transition, request_event=request_event,
                actor=requested_by, rule_id=rule_id, branch_id=branch_id,
            )
        return {
            **summary,
            "already_settled": False,
            "request_event_id": request_event.id,
            **result,
        }

    # --- per-kind semantics -------------------------------------------------

    def _settle_task_closure(
        self,
        candidate: dict[str, Any],
        transition: str,
        *,
        request_event: "Event",
        actor: str,
        rule_id: str,
        reason: str,
        branch_id: str,
    ) -> dict[str, Any]:
        candidate_id = str(candidate["id"])
        if transition == "applied":
            applied = self.service.reconciler.apply_closure_candidate(
                candidate, branch_id=branch_id, actor=actor, rule_id=rule_id,
                settle_source_event_id=request_event.id,
            )
            if not applied:
                raise ValueError(
                    "closure cannot apply: the entry is no longer present in "
                    "open_tasks"
                )
            return {"entry": str(candidate["normalized_content"])}
        if transition == "reverted":
            result = self.service.reconciler.undo_closure(
                candidate_id, branch_id=branch_id, rule_id=rule_id,
                detail={"reason": reason}, actor=actor,
                settle_source_event_id=request_event.id,
            )
            return {
                "entry": result["entry"],
                "transition_id": result["transition_id"],
            }
        transition_id = self.store.settle_candidate(
            candidate_id, "contested",
            source_event_id=request_event.id, actor=actor, rule_id=rule_id,
        )
        return {"transition_id": transition_id}

    def _settle_block_change(
        self,
        candidate: dict[str, Any],
        transition: str,
        *,
        request_event: "Event",
        actor: str,
        rule_id: str,
        branch_id: str,
    ) -> dict[str, Any]:
        candidate_id = str(candidate["id"])
        if transition == "contested":
            transition_id = self.store.settle_candidate(
                candidate_id, "contested",
                source_event_id=request_event.id, actor=actor, rule_id=rule_id,
            )
            return {"transition_id": transition_id}
        request_source = self.store.get_event(str(candidate["source_event_id"]))
        block_name = str(
            request_source.payload.get("block") or candidate["memory_type"]
        )
        proposed = str(
            request_source.payload.get("content")
            or candidate["normalized_content"]
        )
        current = self.store.get_active_blocks(branch_id=branch_id).get(block_name)
        current_content = current.content if current else ""
        if transition == "applied":
            merged = EvidenceConsolidator._merge_block(
                current_content, proposed,
                replace=block_name in _REPLACE_BLOCKS,
            )
            applied_events, _ = self.store.append_internal_events_once(
                f"block-change-applied:{candidate_id}",
                [
                    {
                        "kind": "state",
                        "role": None,
                        "content": f"block change applied: {block_name}",
                        "payload": {
                            "operation": "block_change_applied",
                            "candidate_id": candidate_id,
                            "block": block_name,
                            "content": proposed,
                            # Recorded so revert can restore replace-mode
                            # blocks losslessly without version archaeology.
                            "previous_content": current_content,
                            "enforcement_level": "recorded_only",
                        },
                    }
                ],
                branch_id=branch_id,
            )
            if merged != current_content:
                self.store.set_active_block(
                    block_name, merged, branch_id=branch_id,
                    source_event_ids=(request_source.id, applied_events[0].id),
                )
            transition_id = self.store.settle_candidate(
                candidate_id, "applied",
                source_event_id=request_event.id, actor=actor, rule_id=rule_id,
            )
            return {"transition_id": transition_id, "block": block_name}
        # reverted: line-level for list blocks (later edits survive),
        # recorded previous content for replace-mode blocks.
        applied_records = [
            event
            for event in self.store.events_by_operation(
                "block_change_applied", branch_id=branch_id
            )
            if event.payload.get("candidate_id") == candidate_id
        ]
        if not applied_records:
            raise ValueError(
                "no applied block change to revert for this candidate"
            )
        reverted_events, _ = self.store.append_internal_events_once(
            f"block-change-reverted:{candidate_id}",
            [
                {
                    "kind": "state",
                    "role": None,
                    "content": f"block change reverted: {block_name}",
                    "payload": {
                        "operation": "block_change_reverted",
                        "candidate_id": candidate_id,
                        "block": block_name,
                        "enforcement_level": "recorded_only",
                    },
                }
            ],
            branch_id=branch_id,
        )
        if block_name in _REPLACE_BLOCKS:
            restored = str(applied_records[-1].payload.get("previous_content", ""))
        else:
            target = _normalized(proposed)
            restored = "\n".join(
                raw
                for raw in current_content.splitlines()
                if _normalized(raw.strip().removeprefix("- ").strip()) != target
            )
        if restored != current_content:
            self.store.set_active_block(
                block_name, restored, branch_id=branch_id,
                source_event_ids=(reverted_events[0].id,),
            )
        transition_id = self.store.settle_candidate(
            candidate_id, "reverted",
            source_event_id=request_event.id, actor=actor, rule_id=rule_id,
        )
        return {"transition_id": transition_id, "block": block_name}
