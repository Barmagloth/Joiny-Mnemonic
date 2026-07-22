from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from joiny_mnemonic.hooks import process_hook
from joiny_mnemonic.plugins import PluginRegistry
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).parent / "runtime"


class InjectedFailure(RuntimeError):
    pass


class ToggleProjection:
    name = "toggle"

    def __init__(self) -> None:
        self.failing = True

    def index(self, record) -> None:
        if self.failing:
            raise RuntimeError("semantic unavailable")

    def project(self, record) -> None:
        if self.failing:
            raise RuntimeError("graph unavailable")


class Stage2AtomicityTest(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = RUNTIME_ROOT / f"stage2-atomic-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.service = MemoryService(
            ":memory:",
            project_root=self.root,
            plugins=PluginRegistry(load_installed=False),
        )
        self.store = self.service.store
        self.receipt = 0

    def tearDown(self) -> None:
        self.service.close()

    def _counts(self) -> tuple[int, ...]:
        tables = (
            "branches",
            "events",
            "block_versions",
            "snapshots",
            "task_versions",
            "task_session_bindings",
        )
        with self.store._lock:
            return tuple(
                int(self.store._conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0])
                for table in tables
            )

    @staticmethod
    def _fail_after(method):
        def failed(*args, **kwargs):
            method(*args, **kwargs)
            raise InjectedFailure("fault after durable step")

        return failed

    @staticmethod
    def _fail_on_call(method, target: int):
        calls = 0

        def failed(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = method(*args, **kwargs)
            if calls == target:
                raise InjectedFailure(f"fault after durable step {target}")
            return result

        return failed

    def _host_user(self, content: str, branch_id: str = "main"):
        self.receipt += 1
        events, _ = self.store.append_host_events_once(
            f"stage2:{self.receipt}",
            [{
                "kind": "message",
                "role": "user",
                "content": content,
                "payload": {"hook_event_name": "UserPromptSubmit"},
            }],
            adapter="claude-code",
            branch_id=branch_id,
        )
        return events[0]

    def _candidate(
        self, content: str = "atomic fact", initial_status: str = "auto"
    ) -> tuple[str, str | None]:
        event = self._host_user(content)
        self.store.register_extractor_config(
            "stage2-fixture", {"name": "stage2-fixture"}
        )
        run_id = self.store.ensure_extraction_run(event.id, "stage2-fixture")
        attempt_no, started_at = self.store.start_extraction_attempt(run_id)
        candidate = SimpleNamespace(
            memory_type="fact",
            normalized_content=content,
            evidence_quote=content,
            evidence_start=0,
            evidence_end=len(content),
            evidence_zone="prose",
            confidence=0.99,
            initial_status=initial_status,
            rule_id="stage2_fixture",
            valid_from=None,
            valid_to=None,
        )
        candidate_id = self.store.commit_extraction_success(
            run_id=run_id,
            attempt_no=attempt_no,
            started_at=started_at,
            event=event,
            candidates=[candidate],
            rejections=[],
            raw_response={},
            extractor_config_hash="stage2-fixture",
        )[0]
        match = self.store.find_auto_candidate_match("fact", content)
        self.assertIsNotNone(match)
        return candidate_id, match[1]

    def test_jm_inv_002_task_start_rolls_back_after_every_step(self) -> None:
        targets = (
            (self.store, "create_branch"),
            (self.store, "append_event"),
            (self.store, "set_active_block"),
            (self.service, "create_snapshot"),
            (self.store, "create_task_version"),
        )
        for index, (owner, name) in enumerate(targets):
            with self.subTest(step=name):
                before = self._counts()
                original = getattr(owner, name)
                with patch.object(owner, name, side_effect=self._fail_after(original)):
                    with self.assertRaises(InjectedFailure):
                        self.service.tasks.start(
                            f"atomic-start-{index}", f"Atomic start {index}"
                        )
                self.assertEqual(self._counts(), before)

    def test_jm_inv_002_task_status_rolls_back_after_every_step(self) -> None:
        task = self.service.tasks.start("atomic-status", "Atomic status")
        evidence = self._host_user("block atomic status", task.branch_id)
        targets = (
            (self.store, "append_internal_events_once"),
            (self.service, "create_snapshot"),
            (self.store, "create_task_version"),
        )
        for owner, name in targets:
            with self.subTest(step=name):
                before = self._counts()
                original = getattr(owner, name)
                with patch.object(owner, name, side_effect=self._fail_after(original)):
                    with self.assertRaises(InjectedFailure):
                        self.service.tasks.set_status(
                            task.task_key, "blocked", source_event_id=evidence.id
                        )
                self.assertEqual(self._counts(), before)
                self.assertEqual(self.store.get_task(task.task_key).version, 1)
        before = self._counts()
        original = self.store.create_task_version
        with patch.object(
            self.store, "create_task_version", side_effect=self._fail_after(original)
        ):
            with self.assertRaises(InjectedFailure):
                self.service.tasks.set_status_as_operator(
                    task.task_key, "blocked", note="operator evidence rollback"
                )
        self.assertEqual(self._counts(), before)

    def test_jm_inv_002_candidate_confirmation_rolls_back_every_step(self) -> None:
        candidate_id, memory_id = self._candidate()
        approval = self._host_user("confirm atomic fact")

        def counts() -> tuple[int, int]:
            with self.store._lock:
                return (
                    int(self.store._conn.execute(
                        "SELECT COUNT(*) FROM candidate_transitions WHERE candidate_id=?",
                        (candidate_id,),
                    ).fetchone()[0]),
                    int(self.store._conn.execute(
                        "SELECT COUNT(*) FROM candidate_memory_links WHERE candidate_id=?",
                        (candidate_id,),
                    ).fetchone()[0]),
                )

        before = counts()
        original = self.store.transition_candidate
        for call in (1, 2):
            with self.subTest(step=f"transition-{call}"):
                with patch.object(
                    self.store,
                    "transition_candidate",
                    side_effect=self._fail_on_call(original, call),
                ):
                    with self.assertRaises(InjectedFailure):
                        self.store.confirm_candidate_match(
                            candidate_id, memory_id, source_event_id=approval.id
                        )
                self.assertEqual(counts(), before)
        values = [
            uuid.UUID(int=1),
            uuid.UUID(int=2),
            InjectedFailure("fault before confirmation link"),
        ]
        with patch("joiny_mnemonic.storage.uuid.uuid4", side_effect=values):
            with self.assertRaises(InjectedFailure):
                self.store.confirm_candidate_match(
                    candidate_id, memory_id, source_event_id=approval.id
                )
        self.assertEqual(counts(), before)

    def test_candidate_confirmation_rolls_back_its_host_event(self) -> None:
        candidate_id, _ = self._candidate(initial_status="quarantined")
        before_events = len(self.store.query_events())
        original = self.store.confirm_candidate_match
        with patch.object(
            self.store,
            "confirm_candidate_match",
            side_effect=self._fail_after(original),
        ):
            with self.assertRaises(InjectedFailure):
                process_hook(
                    self.service,
                    "claude-code",
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "stage2-confirmation",
                        "prompt": "Fact: atomic fact",
                    },
                )
        self.assertEqual(len(self.store.query_events()), before_events)
        candidate = next(
            item for item in self.store.list_extraction_candidates()
            if item.id == candidate_id
        )
        self.assertEqual(candidate.current_status, "quarantined")

    def test_jm_inv_002_quarantined_confirmation_materializes_atomically(self) -> None:
        candidate_id, memory_id = self._candidate(
            "quarantined atomic fact", "quarantined"
        )
        self.assertIsNone(memory_id)
        approval = self._host_user("confirm quarantined atomic fact")

        def counts() -> tuple[int, int, int, int]:
            with self.store._lock:
                return tuple(int(self.store._conn.execute(sql, (candidate_id,)).fetchone()[0])
                    for sql in (
                        "SELECT COUNT(*) FROM candidate_transitions WHERE candidate_id=?",
                        "SELECT COUNT(*) FROM candidate_memory_links WHERE candidate_id=?",
                        "SELECT COUNT(*) FROM memory_records WHERE metadata_json LIKE '%' || ? || '%'",
                        "SELECT COUNT(*) FROM events WHERE payload_json LIKE '%' || ? || '%'",
                    ))

        before = counts()
        original_transition = self.store.transition_candidate
        for call in (1, 2):
            with self.subTest(step=f"transition-{call}"):
                with patch.object(
                    self.store, "transition_candidate",
                    side_effect=self._fail_on_call(original_transition, call),
                ):
                    with self.assertRaises(InjectedFailure):
                        self.store.confirm_candidate_match(
                            candidate_id, None, source_event_id=approval.id
                        )
                self.assertEqual(counts(), before)
        for name in ("derive_memory", "_link_confirmed_candidate"):
            with self.subTest(step=name):
                original = getattr(self.store, name)
                with patch.object(
                    self.store, name, side_effect=self._fail_after(original)
                ):
                    with self.assertRaises(InjectedFailure):
                        self.store.confirm_candidate_match(
                            candidate_id, None, source_event_id=approval.id
                        )
                self.assertEqual(counts(), before)
        created_id = self.store.confirm_candidate_match(
            candidate_id, None, source_event_id=approval.id
        )
        record = self.store.get_memory(created_id)
        self.assertEqual(record.content, "quarantined atomic fact")
        self.assertEqual(record.metadata["authority_level"], "confirmed")

    def test_jm_inv_002_settlement_apply_rolls_back_every_step(self) -> None:
        requested, _ = self.store.append_internal_events_once(
            "stage2:block-request",
            [{
                "kind": "state",
                "role": None,
                "content": "block change requested",
                "payload": {
                    "operation": "block_change_requested",
                    "block": "goal",
                    "content": "new goal",
                },
            }],
        )
        candidate_id, _, _ = self.store.create_settlement_candidate(
            kind="block_change",
            content="new goal",
            source_event_id=requested[0].id,
            memory_type="goal",
        )
        before_events = len(self.store.query_events())
        targets = (
            (self.store, "append_internal_events_once", 1),
            (self.store, "append_internal_events_once", 2),
            (self.store, "set_active_block", 1),
            (self.store, "settle_candidate", 1),
        )
        for owner, name, call in targets:
            with self.subTest(step=name, call=call):
                original = getattr(owner, name)
                with patch.object(
                    owner, name, side_effect=self._fail_on_call(original, call)
                ):
                    with self.assertRaises(InjectedFailure):
                        self.service.settlement.settle(
                            candidate_id, "applied",
                            reason="approved locally", requested_by="operator",
                        )
                self.assertEqual(len(self.store.query_events()), before_events)
                self.assertNotIn("goal", self.store.get_active_blocks())
                self.assertEqual(
                    self.store.get_settlement_candidate(candidate_id)["status"],
                    "pending",
                )

    def test_jm_inv_002_settlement_revert_rolls_back_every_step(self) -> None:
        source = self.store.append_event(kind="message", content="old goal")
        self.store.set_active_block(
            "goal", "old goal", source_event_ids=(source.id,)
        )
        requested, _ = self.store.append_internal_events_once(
            "stage2:block-revert-request",
            [{
                "kind": "state",
                "role": None,
                "content": "block change requested",
                "payload": {
                    "operation": "block_change_requested",
                    "block": "goal",
                    "content": "new goal",
                },
            }],
        )
        candidate_id, _, _ = self.store.create_settlement_candidate(
            kind="block_change",
            content="new goal",
            source_event_id=requested[0].id,
            memory_type="goal",
        )
        self.service.settlement.settle(
            candidate_id,
            "applied",
            reason="apply before revert",
            requested_by="operator",
        )
        before_events = len(self.store.query_events())
        targets = (
            (self.store, "append_internal_events_once", 1),
            (self.store, "append_internal_events_once", 2),
            (self.store, "set_active_block", 1),
            (self.store, "settle_candidate", 1),
        )
        for owner, name, call in targets:
            with self.subTest(step=name, call=call):
                original = getattr(owner, name)
                with patch.object(
                    owner, name, side_effect=self._fail_on_call(original, call)
                ):
                    with self.assertRaises(InjectedFailure):
                        self.service.settlement.settle(
                            candidate_id,
                            "reverted",
                            reason="revert atomically",
                            requested_by="operator",
                        )
                self.assertEqual(len(self.store.query_events()), before_events)
                self.assertEqual(
                    self.store.get_active_blocks()["goal"].content, "new goal"
                )
                self.assertEqual(
                    self.store.get_settlement_candidate(candidate_id)["status"],
                    "applied",
                )

    def test_jm_inv_008_event_survives_extraction_and_witness_failures(self) -> None:
        self.service.extraction.enabled = True
        self.service.extraction.last_wakeup_error = "worker launch failed"
        failed_witness = {
            "status": "registry_update_failed",
            "details": {"error": "PermissionError"},
        }
        healthy_witness = {"status": "valid_extension", "details": {}}
        with (
            patch.object(
                self.service.extraction, "notify", side_effect=(False, True)
            ),
            patch.object(
                self.service,
                "checkpoint_witness",
                side_effect=(failed_witness, healthy_witness),
            ),
        ):
            event = self.service.append_event(
                kind="message", role="user", content="canonical survives"
            )
            self.assertEqual(self.store.get_event(event.id).content, "canonical survives")
            self.assertEqual(
                {item["system"] for item in self.service.projection_failures.pending()},
                {"extraction:wakeup", "witness:registry"},
            )
            result = self.service.projection_failures.retry()
        self.assertEqual(result, {"pending": 2, "recovered": 2, "failed": 0})
        self.assertEqual(self.service.projection_failures.pending(), ())

    def test_jm_inv_008_projection_failure_is_durable_and_retryable(self) -> None:
        plugin = ToggleProjection()
        self.service.plugins.register_semantic(plugin)
        self.service.plugins.register_knowledge_graph(plugin)
        source = self.store.append_event(
            kind="message", role="user", content="projection source"
        )
        record = self.service.derive_memory(
            memory_type="fact", content="durable core record",
            source_event_ids=(source.id,),
        )
        self.assertEqual(self.store.get_memory(record.id).content, "durable core record")
        pending = self.service.projection_failures.pending()
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(item["retryable"] for item in pending))
        first_keys = {item["failure_key"] for item in pending}
        self.service.projection_failures.project_memory(record)
        self.assertEqual(
            {item["failure_key"] for item in self.service.projection_failures.pending()},
            first_keys,
        )
        plugin.failing = False
        result = self.service.projection_failures.retry()
        self.assertEqual(result, {"pending": 2, "recovered": 2, "failed": 0})
        self.assertEqual(self.service.projection_failures.pending(), ())
        plugin.failing = True
        self.service.projection_failures.project_memory(record)
        second_keys = {
            item["failure_key"] for item in self.service.projection_failures.pending()
        }
        self.assertEqual(len(second_keys), 2)
        self.assertTrue(first_keys.isdisjoint(second_keys))
        self.assertEqual(
            len(self.store.events_by_operation("derived_projection_failed")), 4
        )

    def test_settlement_changes_workstream_obligations(self) -> None:
        task = self.service.tasks.start("settled-obligation", "Settled obligation")
        evidence = self._host_user("the entry is complete", task.branch_id)
        self.store.set_active_block(
            "open_tasks", "- ship atomic stage",
            branch_id=task.branch_id, source_event_ids=(evidence.id,),
        )
        detected, _ = self.store.append_internal_events_once(
            "stage2:closure-detected",
            [{
                "kind": "state",
                "role": None,
                "content": "task completion detected",
                "payload": {
                    "operation": "task_completion_detected",
                    "entry": "ship atomic stage",
                    "evidence_event_id": evidence.id,
                    "evidence_kind": "explicit",
                    "evidence_detail": "test",
                },
            }],
            branch_id=task.branch_id,
        )
        candidate_id, _, _ = self.store.create_settlement_candidate(
            kind="task_closure",
            content="ship atomic stage",
            source_event_id=detected[0].id,
            evidence_event_id=evidence.id,
        )
        self.assertEqual(
            {item["id"] for item in self.service.tasks.obligations(task.task_key)},
            {
                f"open_tasks:{self.store.get_active_blocks(branch_id=task.branch_id)['open_tasks'].id}:0",
                candidate_id,
            },
        )
        self.service.settlement.settle(
            candidate_id,
            "applied",
            reason="close the accepted obligation",
            requested_by="operator",
            branch_id=task.branch_id,
        )
        self.assertEqual(self.service.tasks.obligations(task.task_key), ())

    def test_completion_fails_before_write_and_exact_override_is_audited(self) -> None:
        task = self.service.tasks.start("atomic-complete", "Atomic complete")
        source = self._host_user("keep this obligation", task.branch_id)
        self.store.set_active_block(
            "open_tasks", "- ship stage two",
            branch_id=task.branch_id, source_event_ids=(source.id,),
        )
        evidence = self._host_user("complete with explicit override", task.branch_id)
        before = self._counts()
        with self.assertRaises(PermissionError):
            self.service.tasks.complete(task.task_key, source_event_id=evidence.id)
        self.assertEqual(self._counts(), before)
        obligations = self.service.tasks.obligations(task.task_key)
        self.assertEqual(len(obligations), 1)
        targets = (
            (self.store, "append_internal_events_once"),
            (self.service, "create_snapshot"),
            (self.store, "create_task_version"),
        )
        for owner, name in targets:
            with self.subTest(step=name):
                before = self._counts()
                original = getattr(owner, name)
                with patch.object(owner, name, side_effect=self._fail_after(original)):
                    with self.assertRaises(InjectedFailure):
                        self.service.tasks.complete(
                            task.task_key,
                            source_event_id=evidence.id,
                            override_obligations=[obligations[0]["id"]],
                            override_reason="owner accepts remaining work",
                        )
                self.assertEqual(self._counts(), before)
                self.assertEqual(self.store.get_task(task.task_key).status, "active")
        completed = self.service.tasks.complete(
            task.task_key,
            source_event_id=evidence.id,
            override_obligations=[obligations[0]["id"]],
            override_reason="owner explicitly accepts the remaining work",
        )
        self.assertEqual(completed.status, "completed")
        event = next(
            item
            for item in reversed(self.store.query_events(branch_id=task.branch_id))
            if item.payload.get("operation") == "workstream_status_changed"
        )
        self.assertEqual(
            event.payload["override_obligations"], [obligations[0]["id"]]
        )
        self.assertEqual(
            event.payload["override_reason"],
            "owner explicitly accepts the remaining work",
        )


if __name__ == "__main__":
    unittest.main()
