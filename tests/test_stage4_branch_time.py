from __future__ import annotations

import time
import unittest
from pathlib import Path

from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.storage import SNAPSHOT_REPLAY_CODE_VERSION


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class Stage4BranchTimeTest(unittest.TestCase):
    """Executable acceptance checks for ROADMAP stage 4."""

    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.store = self.service.store

    def tearDown(self) -> None:
        self.service.close()

    def _event(self, content: str, *, branch_id: str = "main"):
        return self.store.append_event(
            kind="message", role="user", content=content, branch_id=branch_id
        )

    def _memory(self, content: str, *, branch_id: str, **temporal):
        source = self._event(f"source: {content}", branch_id=branch_id)
        record = self.service.derive_memory(
            memory_type="fact",
            content=content,
            source_event_ids=(source.id,),
            branch_id=branch_id,
            **temporal,
        )
        return record

    def _three_generation_lineage(self):
        root = self._event("root before child fork")
        self.store.create_branch(
            "stage4/child", parent_id="main", fork_event_seq=root.seq
        )
        child = self._event("child before grandchild fork", branch_id="stage4/child")
        self.store.create_branch(
            "stage4/grandchild",
            parent_id="stage4/child",
            fork_event_seq=child.seq,
        )
        return root, child

    def test_01_child_sees_parent_only_through_fork_event_seq(self) -> None:
        root, child = self._three_generation_lineage()

        lineage = dict(self.store.branch_lineage("stage4/grandchild"))
        visible_ids = {
            event.id
            for event in self.store.query_events(branch_id="stage4/grandchild")
        }

        self.assertEqual(lineage["main"], root.seq)
        self.assertEqual(lineage["stage4/child"], child.seq)
        self.assertIsNone(lineage["stage4/grandchild"])
        self.assertEqual(visible_ids, {root.id, child.id})

    def test_02_late_parent_event_is_absent_from_descendant(self) -> None:
        root, child = self._three_generation_lineage()
        late_root = self._event("root after child fork")
        late_child = self._event(
            "child after grandchild fork", branch_id="stage4/child"
        )
        grandchild = self._event(
            "grandchild local event", branch_id="stage4/grandchild"
        )

        visible_ids = {
            event.id
            for event in self.store.query_events(branch_id="stage4/grandchild")
        }

        self.assertEqual(visible_ids, {root.id, child.id, grandchild.id})
        self.assertNotIn(late_root.id, visible_ids)
        self.assertNotIn(late_child.id, visible_ids)

    def test_03_task_mutation_stays_on_its_assigned_branch(self) -> None:
        self._event("root task context")
        self.store.create_branch("stage4/task-parent", parent_id="main")
        task = self.service.tasks.start(
            "STAGE4-TASK",
            "Verify task branch binding",
            parent_branch="stage4/task-parent",
        )
        self.assertEqual(
            tuple(branch for branch, _ in self.store.branch_lineage(task.branch_id)),
            ("main", "stage4/task-parent", task.branch_id),
        )

        wrong_branch_source = self._event(
            "attempted mutation from parent", branch_id="stage4/task-parent"
        )
        with self.assertRaisesRegex(
            ValueError, "task key is already assigned to another branch"
        ):
            self.store.create_task_version(
                task_key=task.task_key,
                branch_id="stage4/task-parent",
                title=task.title,
                status="blocked",
                source_event_ids=(wrong_branch_source.id,),
            )

        blocked = self.service.tasks.set_status(
            task.task_key, "blocked", note="waiting for lineage review"
        )
        transition_events = self.store.query_events(branch_id=task.branch_id)
        self.assertEqual(blocked.branch_id, task.branch_id)
        self.assertTrue(
            any(
                event.payload.get("task", {}).get("status") == "blocked"
                for event in transition_events
            )
        )
        self.assertFalse(
            any(
                event.payload.get("task", {}).get("status") == "blocked"
                for event in self.store.query_events(
                    branch_id="stage4/task-parent"
                )
            )
        )

    def test_04_parent_completion_does_not_complete_distinct_child_task(self) -> None:
        parent = self.service.tasks.start("PARENT-4", "Parent task")
        child = self.service.tasks.start(
            "CHILD-4",
            "Child task",
            parent_task_key=parent.task_key,
        )
        self.assertEqual(
            tuple(branch for branch, _ in self.store.branch_lineage(child.branch_id)),
            ("main", parent.branch_id, child.branch_id),
        )
        approval = self.store.append_host_event(
            adapter="codex",
            branch_id=parent.branch_id,
            kind="message",
            role="user",
            content="complete the parent task",
        )

        completed_parent = self.service.tasks.complete(
            parent.task_key, source_event_id=approval.id
        )
        unchanged_child = self.store.get_task(child.task_key)

        self.assertEqual(completed_parent.status, "completed")
        self.assertEqual(unchanged_child.status, "active")
        self.assertEqual(unchanged_child.version, child.version)
        self.assertNotEqual(completed_parent.task_key, unchanged_child.task_key)

    def test_05_known_at_limits_event_seq_and_branch_visibility(self) -> None:
        root_record = self._memory("root knowledge", branch_id="main")
        root_known_at = self.store.query_events(branch_id="main")[-1].created_at
        time.sleep(0.01)
        root_tip = self.store.query_events(branch_id="main")[-1]
        self.store.create_branch(
            "stage4/child", parent_id="main", fork_event_seq=root_tip.seq
        )
        child_record = self._memory("child knowledge", branch_id="stage4/child")
        child_tip = self.store.query_events(branch_id="stage4/child")[-1]
        self.store.create_branch(
            "stage4/grandchild",
            parent_id="stage4/child",
            fork_event_seq=child_tip.seq,
        )
        grandchild_record = self._memory(
            "grandchild knowledge", branch_id="stage4/grandchild"
        )
        hidden_record = self._memory(
            "late child knowledge", branch_id="stage4/child"
        )

        early_hits = self.service.search(
            branch_id="stage4/grandchild",
            known_at=root_known_at,
            include_events=False,
            include_unknown_validity=True,
            semantic=False,
            record_telemetry=False,
            limit=20,
        )
        late_hits = self.service.search(
            branch_id="stage4/grandchild",
            known_at=self.store.get_memory(hidden_record.id).created_at,
            include_events=False,
            include_unknown_validity=True,
            semantic=False,
            record_telemetry=False,
            limit=20,
        )

        self.assertEqual({hit.id for hit in early_hits}, {root_record.id})
        self.assertEqual(
            {hit.id for hit in late_hits},
            {root_record.id, child_record.id, grandchild_record.id},
        )
        self.assertNotIn(hidden_record.id, {hit.id for hit in late_hits})

    def test_06_valid_at_filters_period_without_expanding_branch_visibility(self) -> None:
        visible_march = self._memory(
            "visible March policy",
            branch_id="main",
            valid_from="2026-03-01T00:00:00+00:00",
            valid_to="2026-04-01T00:00:00+00:00",
        )
        visible_april = self._memory(
            "visible April policy",
            branch_id="main",
            valid_from="2026-04-01T00:00:00+00:00",
            valid_to="2026-05-01T00:00:00+00:00",
        )
        root_tip = self.store.query_events(branch_id="main")[-1]
        self.store.create_branch(
            "stage4/child", parent_id="main", fork_event_seq=root_tip.seq
        )
        child_tip = self._event("child fork point", branch_id="stage4/child")
        self.store.create_branch(
            "stage4/grandchild",
            parent_id="stage4/child",
            fork_event_seq=child_tip.seq,
        )
        hidden_march = self._memory(
            "hidden March policy",
            branch_id="stage4/child",
            valid_from="2026-03-01T00:00:00+00:00",
            valid_to="2026-04-01T00:00:00+00:00",
        )

        hits = self.service.search(
            branch_id="stage4/grandchild",
            valid_at="2026-03-15T00:00:00+00:00",
            include_events=False,
            semantic=False,
            record_telemetry=False,
            limit=20,
        )
        ids = {hit.id for hit in hits}

        self.assertIn(visible_march.id, ids)
        self.assertNotIn(visible_april.id, ids)
        self.assertNotIn(hidden_march.id, ids)

    def test_07_snapshot_binds_branch_cursor_and_replay_version(self) -> None:
        self._three_generation_lineage()
        tip = self._event(
            "grandchild snapshot tip", branch_id="stage4/grandchild"
        )

        snapshot = self.service.create_snapshot(
            branch_id="stage4/grandchild", tracked_files=[]
        )

        self.assertEqual(snapshot.branch_id, "stage4/grandchild")
        self.assertEqual(snapshot.cursor_seq, tip.seq)
        self.assertEqual(
            snapshot.replay_code_version, SNAPSHOT_REPLAY_CODE_VERSION
        )

    def test_08_rebuilding_unchanged_snapshot_repeats_state_hash(self) -> None:
        self._three_generation_lineage()
        self._event("stable grandchild state", branch_id="stage4/grandchild")

        first = self.service.create_snapshot(
            branch_id="stage4/grandchild", tracked_files=[]
        )
        second = self.service.create_snapshot(
            branch_id="stage4/grandchild", tracked_files=[]
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.state_sha256, second.state_sha256)


if __name__ == "__main__":
    unittest.main()
