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

    def test_file_evidence_auto_closes_even_with_flag_off(self) -> None:
        """task6.md 6B: strong evidence (trusted host write of the exact
        path) auto-applies BY DEFAULT — cheap lossless undo licenses it.
        The legacy flag now gates only medium (command) evidence."""
        self.service.initialize_project()  # flag off
        self._open_task("создать файл delme2.md")
        self._write_evidence("R:\\Projects\\GPTShared\\delme2.md")

        summary = self.service.reconciler.reconcile()
        self.assertEqual(summary["detected"], 1)
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(len(summary["auto_closed"]), 1)
        auto = summary["auto_closed"][0]
        self.assertEqual(auto["entry"], "создать файл delme2.md")

        detections = [
            event for event in self.store.query_events(kinds=("state",))
            if event.payload.get("operation") == "task_completion_detected"
        ]
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].origin_channel, "internal")
        self.assertEqual(detections[0].payload["evidence_kind"], "file")
        self.assertEqual(
            auto["evidence_event_id"], detections[0].payload["evidence_event_id"]
        )
        block = self.store.get_active_blocks().get("open_tasks")
        self.assertTrue(block is None or "delme2.md" not in block.content)
        candidates = self.store.list_settlement_candidates(kind="task_closure")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["status"], "applied")
        self.assertEqual(self.service.reconciler.pending_completions(), [])

        # Idempotent: rerunning emits no second detection and no re-close.
        again = self.service.reconciler.reconcile()
        self.assertEqual(again["closed"], 0)
        detections_after = [
            event for event in self.store.query_events(kinds=("state",))
            if event.payload.get("operation") == "task_completion_detected"
        ]
        self.assertEqual(len(detections_after), 1)

    def test_command_evidence_stays_pending_when_flag_off(self) -> None:
        """Medium evidence keeps the legacy consent gate: detection is
        recorded, the entry stays, pending surfaces with candidate id."""
        self.service.initialize_project()  # flag off
        self._open_task("прогнать `pytest -q` перед релизом")
        self.store.append_host_events_once(
            "evidence:pytest-flag-off",
            [
                {
                    "kind": "tool_output", "role": "tool", "content": "5 passed",
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
        self.assertEqual(summary["detected"], 1)
        self.assertEqual(summary["closed"], 0)
        self.assertEqual(summary["pending"], 1)
        block = self.store.get_active_blocks()["open_tasks"]
        self.assertIn("pytest", block.content)
        pending = self.service.reconciler.pending_completions()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["candidate_id"].startswith("cand_"))
        candidates = self.store.list_settlement_candidates(
            kind="task_closure", status="pending"
        )
        self.assertEqual(len(candidates), 1)
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

    def test_command_prefix_matches_but_embedding_does_not(self) -> None:
        """Review L1: `pytest -q` completes on "pytest -q --tb=short" but a
        command merely containing the string is not evidence."""
        self.service.initialize_project(automatic_task_closure_enabled=True)
        self._open_task("прогнать `pytest -q` в CI")
        self.store.append_host_events_once(
            "evidence:echoed",
            [
                {
                    "kind": "tool_output", "role": "tool", "content": "never",
                    "payload": {
                        "tool_name": "Bash", "hook_event_name": "PostToolUse",
                        "tool_input": {"command": "echo never run pytest -q"},
                    },
                }
            ],
            adapter="claude-code",
        )
        self.assertEqual(self.service.reconciler.reconcile()["detected"], 0)
        self.store.append_host_events_once(
            "evidence:prefixed",
            [
                {
                    "kind": "tool_output", "role": "tool", "content": "ok",
                    "payload": {
                        "tool_name": "Bash", "hook_event_name": "PostToolUse",
                        "tool_input": {"command": "pytest -q --tb=short"},
                    },
                }
            ],
            adapter="claude-code",
        )
        self.assertEqual(self.service.reconciler.reconcile()["closed"], 1)

    def test_closure_preserves_untouched_entry_formatting(self) -> None:
        """Review L2: closing one entry must not reformat the others."""
        self.service.initialize_project(automatic_task_closure_enabled=True)
        self._open_task("создать файл alpha.md")
        anchor = self.store.get_active_blocks()["open_tasks"]
        # A hand-formatted entry through the explicit block API.
        self.store.set_active_block(
            "open_tasks",
            anchor.content + "\n  * задача с ручным форматированием",
            source_event_ids=anchor.source_event_ids,
        )
        self._write_evidence("alpha.md")
        self.service.reconciler.reconcile()
        block = self.store.get_active_blocks()["open_tasks"]
        self.assertNotIn("alpha.md", block.content)
        self.assertIn("  * задача с ручным форматированием", block.content)

    def test_resume_packet_carries_pending_completion_line(self) -> None:
        self.service.initialize_project()  # flag off -> command evidence pends
        self._open_task("прогнать `make docs` перед релизом")
        self.store.append_host_events_once(
            "evidence:make-docs",
            [
                {
                    "kind": "tool_output", "role": "tool", "content": "built",
                    "payload": {
                        "tool_name": "Bash",
                        "hook_event_name": "PostToolUse",
                        "tool_input": {"command": "make docs"},
                    },
                }
            ],
            adapter="claude-code",
        )
        self.service.reconciler.reconcile()
        packet = self.service.resume(token_budget=1500)
        self.assertIn("STATE MAINTENANCE - PENDING CONFIRMATIONS", packet.text)
        self.assertIn("make docs", packet.text)
        # Provenance phrasing, not a bare TODO or an injected imperative.
        self.assertIn("ask the user before treating it as closed", packet.text)

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
