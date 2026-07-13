from __future__ import annotations

import unittest
from pathlib import Path

from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class ReconcilerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.store = self.service.store

    def tearDown(self) -> None:
        self.service.close()

    def _open_task(self, text: str):
        """User marker via the trusted host path: creates task memory + block."""
        events, _ = self.store.append_host_events_once(
            f"receipt:{text}",
            [
                {
                    "kind": "message", "role": "user",
                    "content": f"TODO: {text}", "payload": {},
                }
            ],
            adapter="claude-code",
        )
        self.service.consolidator.consolidate_event(self.service, events[0])
        return events[0]

    def _write_evidence(self, path: str, *, hook_event: str = "PostToolUse"):
        """Evidence through the trusted host-hook channel, as captured live."""
        events, _ = self.store.append_host_events_once(
            f"evidence:{path}:{hook_event}",
            [
                {
                    "kind": "tool_output",
                    "role": "tool",
                    "content": f'{{"type": "create", "filePath": "{path}"}}',
                    "payload": {
                        "tool_name": "Write",
                        "hook_event_name": hook_event,
                        "tool_response": {"type": "create"},
                    },
                    "files": [path],
                }
            ],
            adapter="claude-code",
        )
        return events[0]

    def _untrusted_evidence(self, path: str):
        """The same shape via the public API: must never count."""
        return self.store.append_event(
            kind="tool_output",
            content=f'{{"type": "create", "filePath": "{path}"}}',
            payload={"tool_name": "Write", "hook_event_name": "PostToolUse"},
            files=(path,),
        )

    def test_file_evidence_detection_and_pending_when_flag_off(self) -> None:
        self.service.initialize_project()
        self._open_task("создать файл delme2.md")
        self._write_evidence("R:\\Projects\\GPTShared\\delme2.md")

        summary = self.service.reconciler.reconcile()
        self.assertEqual(summary["detected"], 1)
        self.assertEqual(summary["closed"], 0)
        self.assertEqual(summary["pending"], 1)

        detections = [
            event for event in self.store.query_events(kinds=("state",))
            if event.payload.get("operation") == "task_completion_detected"
        ]
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].origin_channel, "internal")
        self.assertEqual(detections[0].payload["evidence_kind"], "file")
        # The entry stays in the block and surfaces as pending.
        block = self.store.get_active_blocks()["open_tasks"]
        self.assertIn("delme2.md", block.content)
        pending = self.service.reconciler.pending_completions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            pending[0]["evidence_event_id"], detections[0].payload["evidence_event_id"]
        )

        # Idempotent: rerunning emits no second detection event.
        self.service.reconciler.reconcile()
        detections_after = [
            event for event in self.store.query_events(kinds=("state",))
            if event.payload.get("operation") == "task_completion_detected"
        ]
        self.assertEqual(len(detections_after), 1)

    def test_flag_on_closes_block_with_provenance_and_supersedes_task(self) -> None:
        self.service.initialize_project(automatic_task_closure_enabled=True)
        self._open_task("создать файл report.md")
        evidence = self._write_evidence("report.md")

        summary = self.service.reconciler.reconcile()
        self.assertEqual(summary["closed"], 1)

        block = self.store.get_active_blocks().get("open_tasks")
        self.assertTrue(block is None or "report.md" not in block.content)
        if block is not None:
            # Closure provenance: the new block version cites the evidence.
            self.assertIn(evidence.id, block.source_event_ids)
        records = self.store.list_memories(memory_types=("task",))
        completed = [r for r in records if r.metadata.get("status") == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].metadata["completed_by"], evidence.id)
        self.assertIsNotNone(completed[0].supersedes_id)
        # Original version survives in history.
        superseded = self.store.list_memories(
            memory_types=("task",), include_superseded=True
        )
        self.assertGreater(len(superseded), len(records))

    def test_delete_verb_tasks_are_skipped(self) -> None:
        self.service.initialize_project(automatic_task_closure_enabled=True)
        self._open_task("удалить файл junk.md")
        self._write_evidence("junk.md")
        summary = self.service.reconciler.reconcile()
        self.assertEqual(summary["detected"], 0)
        self.assertIn("junk.md", self.store.get_active_blocks()["open_tasks"].content)

    def test_command_evidence_requires_completed_host_output(self) -> None:
        self.service.initialize_project(automatic_task_closure_enabled=True)
        self._open_task("прогнать `pytest -q` перед релизом")
        events, _ = self.store.append_host_events_once(
            "evidence:pytest",
            [
                {
                    "kind": "tool_output", "role": "tool",
                    "content": "5 passed",
                    "payload": {
                        "tool_name": "Bash",
                        "hook_event_name": "PostToolUse",
                        "tool_input": {"command": "pytest -q"},
                    },
                }
            ],
            adapter="claude-code",
        )
        summary = self.service.reconciler.reconcile()
        self.assertEqual(summary["closed"], 1)

    def test_untrusted_and_failed_evidence_never_close(self) -> None:
        """Review findings H1: public-API appends and captured failures are
        not completion evidence, however well-shaped."""
        self.service.initialize_project(automatic_task_closure_enabled=True)
        self._open_task("создать файл secure.md")
        self._untrusted_evidence("secure.md")
        self._write_evidence("secure.md", hook_event="PostToolUseFailure")
        summary = self.service.reconciler.reconcile()
        self.assertEqual(summary["detected"], 0)
        self.assertIn("secure.md", self.store.get_active_blocks()["open_tasks"].content)

    def test_basename_needs_a_segment_boundary(self) -> None:
        """Review finding H2: 'config.py' must not be completed by a write
        to 'test_config.py'."""
        self.service.initialize_project(automatic_task_closure_enabled=True)
        self._open_task("создать файл config.py")
        self._write_evidence("tests/test_config.py")
        summary = self.service.reconciler.reconcile()
        self.assertEqual(summary["detected"], 0)

    def test_question_marker_routes_to_block_change_requested(self) -> None:
        self.service.initialize_project()
        events, _ = self.store.append_host_events_once(
            "receipt:question",
            [
                {
                    "kind": "message", "role": "user",
                    "content": "DECISION: что сделано последним в этом проекте?",
                    "payload": {},
                }
            ],
            adapter="claude-code",
        )
        result = self.service.consolidator.consolidate_event(self.service, events[0])
        # Searchable record is still created; the block is not.
        self.assertEqual(len(result.memory_ids), 1)
        self.assertEqual(result.block_ids, ())
        self.assertIn("decisions", result.skipped_blocks)
        blocks = self.store.get_active_blocks()
        self.assertNotIn("decisions", blocks)
        requests = [
            event for event in self.store.query_events(kinds=("state",))
            if event.payload.get("operation") == "block_change_requested"
        ]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].payload["reason"], "marker_content_is_a_question")
        self.assertEqual(requests[0].payload["source_event_id"], events[0].id)

    def test_hygiene_findings(self) -> None:
        self.service.initialize_project()
        # A '?' entry planted through the explicit block API (pre-guard data).
        anchor = self.store.append_event(kind="message", role="user", content="anchor")
        self.store.set_active_block(
            "decisions", "- что сделано последний в этом проекте?",
            source_event_ids=(anchor.id,),
        )
        findings = self.service.reconciler.hygiene_findings()
        kinds = {item["finding"] for item in findings}
        self.assertIn("decision_entry_is_a_question", kinds)

    def test_memory_blocks_tool_returns_verbatim_protected_state(self) -> None:
        from joiny_mnemonic.mcp import MCPServer

        self.service.initialize_project()
        self._open_task("создать файл quoted.md")
        server = MCPServer(self.service)
        server.handle(
            {
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        )
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("memory_blocks", names)
        response = server.handle(
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "memory_blocks", "arguments": {}},
            }
        )
        blocks = response["result"]["structuredContent"]
        self.assertIn("создать файл quoted.md", blocks["open_tasks"]["content"])
        self.assertTrue(blocks["open_tasks"]["source_event_ids"])

    def test_capabilities_expose_state_maintenance(self) -> None:
        self.service.initialize_project()
        state = self.service.capabilities()["core"]["state_maintenance"]
        self.assertFalse(state["automatic_task_closure_enabled"])
        self.assertEqual(state["pending_task_completions"], [])


if __name__ == "__main__":
    unittest.main()
