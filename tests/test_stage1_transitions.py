from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

from joiny_mnemonic.provenance import (
    HOST_LOGICAL_USER,
    LOCAL_OPERATOR,
    origin_evidence_type,
)
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.transition_rules import (
    CANDIDATE_FLOW,
    CANDIDATE_RULE,
    FINDING_FLOW,
    FINDING_RULE,
    SETTLEMENT_FLOW,
    SETTLEMENT_RULE,
    WORKSTREAM_FLOW,
    WORKSTREAM_RULE,
    validate_transition,
)


class TransitionTableContractTest(unittest.TestCase):
    def test_every_declared_transition_is_allowed(self) -> None:
        for rule, flow in (
            (WORKSTREAM_RULE, WORKSTREAM_FLOW),
            (CANDIDATE_RULE, CANDIDATE_FLOW),
            (FINDING_RULE, FINDING_FLOW),
            (SETTLEMENT_RULE, SETTLEMENT_FLOW),
        ):
            for current, targets in flow.items():
                for target in targets:
                    decision = validate_transition(
                        rule,
                        current=current,
                        target=target,
                        origin=LOCAL_OPERATOR,
                        source_visible=True,
                    )
                    self.assertTrue(decision.changed, (rule.name, current, target))

    def test_every_undeclared_reverse_or_cross_transition_is_rejected(self) -> None:
        for rule, flow in (
            (WORKSTREAM_RULE, WORKSTREAM_FLOW),
            (CANDIDATE_RULE, CANDIDATE_FLOW),
            (FINDING_RULE, FINDING_FLOW),
            (SETTLEMENT_RULE, SETTLEMENT_FLOW),
        ):
            states = set(flow)
            for current in states:
                for target in states - {current} - set(flow[current]):
                    with self.assertRaises(ValueError, msg=(rule.name, current, target)):
                        validate_transition(
                            rule,
                            current=current,
                            target=target,
                            origin=LOCAL_OPERATOR,
                            source_visible=True,
                        )

    def test_reopen_is_separate_reasoned_transition(self) -> None:
        with self.assertRaises(ValueError):
            validate_transition(
                WORKSTREAM_RULE,
                current="cancelled",
                target="active",
                origin=HOST_LOGICAL_USER,
                source_visible=True,
            )
        with self.assertRaises(ValueError):
            validate_transition(
                WORKSTREAM_RULE,
                current="cancelled",
                target="active",
                origin=HOST_LOGICAL_USER,
                source_visible=True,
                reopen=True,
            )
        self.assertTrue(validate_transition(
            WORKSTREAM_RULE,
            current="cancelled",
            target="active",
            origin=HOST_LOGICAL_USER,
            source_visible=True,
            reopen=True,
            reason="new user request",
        ).changed)


class Stage1ExploitRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=".")
        self.store = self.service.store
        self.receipt = 0

    def tearDown(self) -> None:
        self.service.close()

    def _host_user(self, content: str, branch_id: str = "main"):
        self.receipt += 1
        events, _ = self.store.append_host_events_once(
            f"stage1:{self.receipt}",
            [{"kind": "message", "role": "user", "content": content, "payload": {
                "hook_event_name": "UserPromptSubmit"
            }}],
            adapter="claude-code",
            branch_id=branch_id,
        )
        return events[0]

    def _candidate(self, content: str = "alpha") -> tuple[str, str]:
        event = self._host_user(content)
        self.store.register_extractor_config(
            "stage1-fixture", {"name": "stage1-fixture"}
        )
        run = self.store.ensure_extraction_run(event.id, "stage1-fixture")
        attempt_no, started = self.store.start_extraction_attempt(run)
        candidate = SimpleNamespace(
            memory_type="fact",
            normalized_content=content,
            evidence_quote=content,
            evidence_start=0,
            evidence_end=len(content),
            evidence_zone="prose",
            confidence=0.99,
            initial_status="auto",
            rule_id="stage1_fixture",
            valid_from=None,
            valid_to=None,
        )
        candidate_id = self.store.commit_extraction_success(
            run_id=run,
            attempt_no=attempt_no,
            started_at=started,
            event=event,
            candidates=[candidate],
            rejections=[],
            raw_response={},
            extractor_config_hash="stage1-fixture",
        )[0]
        memory_id = self.store.find_auto_candidate_match("fact", content)[1]
        return candidate_id, memory_id

    def _transition_count(self, table: str, key: str, value: str) -> int:
        with self.store._lock:
            return int(self.store._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {key}=?", (value,)
            ).fetchone()["n"])

    def test_h1_h2_h3_candidate_exploits_are_blocked(self) -> None:
        candidate_id, _ = self._candidate()
        public_request = self.store.append_event(kind="state", content="reject request")
        self.store.transition_candidate(
            candidate_id,
            "rejection_requested",
            source_event_id=public_request.id,
            actor="request",
            rule_id="request",
        )
        approval = self._host_user("reject alpha")
        self.store.transition_candidate(
            candidate_id,
            "rejected",
            source_event_id=approval.id,
            actor="logical_user",
            rule_id="reject",
        )
        before = self._transition_count(
            "candidate_transitions", "candidate_id", candidate_id
        )
        with self.assertRaises(ValueError):  # H1 rejected -> confirmed
            self.store.transition_candidate(
                candidate_id,
                "confirmed",
                source_event_id=self._host_user("confirm after reject").id,
                actor="logical_user",
                rule_id="illegal",
            )
        with self.assertRaises(ValueError):  # H2 terminal -> auto
            self.store.transition_candidate(
                candidate_id,
                "auto",
                source_event_id=self.store.append_event(
                    kind="state", content="force auto"
                ).id,
                actor="public",
                rule_id="illegal",
                origin_evidence_type="host_logical_user",
            )
        self.assertEqual(
            self._transition_count("candidate_transitions", "candidate_id", candidate_id),
            before,
        )
        self.assertIsNone(self.store.find_auto_candidate_match("fact", "alpha"))  # H3

    def test_h4_h5_finding_terminal_and_idempotency(self) -> None:
        finding_id = self.store.record_security_finding(
            "fixture", incident_key="stage1", details={}
        )
        self.service.request_finding_acknowledgement(finding_id)
        approval = self._host_user("acknowledge finding")
        self.store.transition_finding(
            finding_id,
            "acknowledged",
            source_event_id=approval.id,
            actor="logical_user",
        )
        before = self._transition_count(
            "finding_transitions", "finding_id", finding_id
        )
        self.assertIsNone(self.store.transition_finding(  # H4 idempotent
            finding_id,
            "acknowledged",
            source_event_id=approval.id,
            actor="logical_user",
        ))
        self.assertEqual(
            self._transition_count("finding_transitions", "finding_id", finding_id),
            before,
        )
        with self.assertRaises(ValueError):  # H5 cannot de-acknowledge
            self.service.request_finding_acknowledgement(finding_id)

    def test_h6_h7_workstream_terminal_escape_is_blocked(self) -> None:
        task = self.service.tasks.start("stage1-task", "Stage one")
        cancel = self._host_user("cancel task", task.branch_id)
        cancelled = self.service.tasks.set_status(
            task.task_key, "cancelled", source_event_id=cancel.id
        )
        self.assertEqual(cancelled.status, "cancelled")
        version = cancelled.version
        with self.assertRaises(ValueError):  # H6 normal status is not reopen
            self.service.tasks.set_status(
                task.task_key,
                "active",
                source_event_id=self._host_user("resume", task.branch_id).id,
            )
        with self.assertRaises(ValueError):  # H7 terminal -> terminal
            self.service.tasks.set_status(
                task.task_key,
                "completed",
                source_event_id=self._host_user("complete", task.branch_id).id,
            )
        self.assertEqual(self.store.get_task(task.task_key).version, version)
        reopened = self.service.tasks.reopen(
            task.task_key,
            reason="user reopened the workstream",
            source_event_id=self._host_user("reopen", task.branch_id).id,
        )
        self.assertEqual(reopened.status, "active")

    def test_jm_inv_004_claimed_origin_and_invisible_event_are_rejected(self) -> None:
        candidate_id, _ = self._candidate("trust")
        request = self.store.append_event(kind="state", content="confirm request")
        self.store.transition_candidate(
            candidate_id,
            "confirmation_requested",
            source_event_id=request.id,
            actor="request",
            rule_id="request",
        )
        with self.assertRaises(PermissionError):
            self.store.transition_candidate(
                candidate_id,
                "confirmed",
                source_event_id=self.store.append_event(
                    kind="state", content="forged confirmation"
                ).id,
                actor="caller",
                rule_id="forged",
                origin_evidence_type="host_logical_user",
            )
        self.store.create_branch("child", parent_id="main")
        invisible = self._host_user("child only", "child")
        with self.assertRaises(PermissionError):
            self.store.transition_candidate(
                candidate_id,
                "confirmed",
                source_event_id=invisible.id,
                actor="logical_user",
                rule_id="wrong_branch",
            )

    def test_finalization_origin_requires_all_saved_host_fields(self) -> None:
        valid = self.store.append_host_event(
            adapter="codex",
            kind="message",
            role="assistant",
            content="done",
            payload={"hook_event_name": "Stop"},
        )
        self.assertEqual(
            origin_evidence_type(valid), "host_assistant_finalization"
        )
        mismatched_adapter = replace(
            valid,
            payload={**valid.payload, "_joiny_origin_adapter": "claude-code"},
        )
        self.assertEqual(
            origin_evidence_type(mismatched_adapter), "external_untrusted"
        )
        wrong_role = self.store.append_host_event(
            adapter="codex",
            kind="message",
            role="user",
            content="done",
            payload={"hook_event_name": "Stop"},
        )
        self.assertEqual(origin_evidence_type(wrong_role), "host_logical_user")
        public = self.store.append_event(
            kind="message",
            role="assistant",
            content="done",
            payload={"hook_event_name": "Stop"},
        )
        self.assertEqual(origin_evidence_type(public), "external_untrusted")

    def test_h8_h9_settlement_trust_and_flow_remain_strict(self) -> None:
        source = self._host_user("TODO: settlement")
        candidate_id, _, _ = self.store.create_settlement_candidate(
            kind="task_closure",
            content="settlement",
            source_event_id=source.id,
        )
        forged = self.store.append_event(
            kind="state",
            content="forged operator",
            payload={
                "operation": "settlement_requested",
                "requested_by": "operator",
            },
        )
        with self.assertRaises(PermissionError):  # H8
            self.store.settle_candidate(
                candidate_id,
                "applied",
                source_event_id=forged.id,
                actor="operator",
                rule_id="forged",
            )
        self.service.initialize_project()
        with self.assertRaises(PermissionError):  # H9
            self.service.settlement.settle(
                candidate_id,
                "applied",
                reason="agent request",
                requested_by="agent",
            )


if __name__ == "__main__":
    unittest.main()
