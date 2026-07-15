"""task6.md 6C: settlement surfaces.

CLI `candidates show/settle`, MCP `memory_candidates` +
`memory_settle_candidate` through the real server handshake, the bounded
resume index of active candidates, and the trust hardening that keeps
manual settlement away from untrusted text: only the local operator CLI, a
host-verified user event, or an explicitly policy-delegated agent can
settle anything (H1 discipline).
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic.cli import build_parser
from joiny_mnemonic.mcp import MCPServer
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class _SettlementFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.store = self.service.store
        self._receipt = 0

    def tearDown(self) -> None:
        self.service.close()

    def _open_task(self, text: str):
        self._receipt += 1
        events, _ = self.store.append_host_events_once(
            f"receipt:{text}:{self._receipt}",
            [{"kind": "message", "role": "user", "content": f"TODO: {text}", "payload": {}}],
            adapter="claude-code",
        )
        self.service.consolidator.consolidate_event(self.service, events[0])
        return events[0]

    def _write_evidence(self, path: str):
        self._receipt += 1
        events, _ = self.store.append_host_events_once(
            f"evidence:{path}:{self._receipt}",
            [
                {
                    "kind": "tool_output", "role": "tool",
                    "content": f'{{"type": "create", "filePath": "{path}"}}',
                    "payload": {
                        "tool_name": "Write",
                        "hook_event_name": "PostToolUse",
                        "tool_response": {"type": "create"},
                    },
                    "files": [path],
                }
            ],
            adapter="claude-code",
        )
        return events[0]

    def _command_evidence(self, command: str):
        self._receipt += 1
        events, _ = self.store.append_host_events_once(
            f"evidence:{command}:{self._receipt}",
            [
                {
                    "kind": "tool_output", "role": "tool", "content": "ok",
                    "payload": {
                        "tool_name": "Bash",
                        "hook_event_name": "PostToolUse",
                        "tool_input": {"command": command},
                    },
                }
            ],
            adapter="claude-code",
        )
        return events[0]

    def _pending_closure_candidate(self) -> str:
        """Flag off + command evidence -> a pending (medium) candidate."""
        self._open_task("прогнать `make docs` перед релизом")
        self._command_evidence("make docs")
        summary = self.service.reconciler.reconcile()
        self.assertEqual(summary["pending"], 1)
        pending = self.store.list_settlement_candidates(
            kind="task_closure", status="pending"
        )
        return str(pending[0]["id"])

    def _block_change_candidate(self) -> str:
        self._receipt += 1
        events, _ = self.store.append_host_events_once(
            f"receipt:question:{self._receipt}",
            [
                {
                    "kind": "message", "role": "user",
                    "content": "DECISION: перейти на schema v10?", "payload": {},
                }
            ],
            adapter="claude-code",
        )
        self.service.consolidator.consolidate_event(self.service, events[0])
        candidates = self.store.list_settlement_candidates(kind="block_change")
        return str(candidates[0]["id"])


class ManualSettlementCase(_SettlementFixture):
    """Service-level settle verbs: effects per kind, fail-closed edges."""

    def test_show_returns_candidate_with_transition_history(self) -> None:
        self.service.initialize_project()
        candidate_id = self._pending_closure_candidate()
        shown = self.service.settlement.show(candidate_id)
        self.assertEqual(shown["id"], candidate_id)
        self.assertEqual(shown["status"], "pending")
        self.assertEqual(shown["candidate_kind"], "task_closure")
        self.assertEqual(len(shown["transitions"]), 1)
        self.assertEqual(shown["transitions"][0]["to_status"], "pending")
        with self.assertRaises(KeyError):
            self.service.settlement.show("cand_missing")

    def test_operator_applies_pending_closure(self) -> None:
        self.service.initialize_project()
        candidate_id = self._pending_closure_candidate()
        result = self.service.settlement.settle(
            candidate_id, "applied",
            reason="доки собраны, подтверждаю",
            requested_by="operator",
        )
        self.assertFalse(result["already_settled"])
        self.assertNotIn(
            "make docs", self.store.get_active_blocks()["open_tasks"].content
        )
        records = self.store.list_memories(memory_types=("task",))
        self.assertEqual(records[0].metadata.get("status"), "completed")
        shown = self.service.settlement.show(candidate_id)
        self.assertEqual(shown["status"], "applied")
        last = shown["transitions"][-1]
        self.assertEqual(last["actor"], "operator")
        self.assertEqual(last["rule_id"], "manual_settle_operator")
        # The transition cites the canonical settlement request event.
        request = self.store.get_event(last["source_event_id"])
        self.assertEqual(request.payload["operation"], "settlement_requested")
        self.assertEqual(request.payload["reason"], "доки собраны, подтверждаю")
        # Idempotent repeat, and terminal edges keep failing closed.
        again = self.service.settlement.settle(
            candidate_id, "applied", reason="повтор", requested_by="operator"
        )
        self.assertTrue(again["already_settled"])

    def test_operator_contests_and_reverts(self) -> None:
        self.service.initialize_project()
        candidate_id = self._pending_closure_candidate()
        block_before = self.store.get_active_blocks()["open_tasks"].content
        contested = self.service.settlement.settle(
            candidate_id, "contested",
            reason="ещё не готово", requested_by="operator",
        )
        self.assertIn("transition_id", contested)
        # Ledger-only: the block is untouched, the entry stays open.
        self.assertEqual(
            self.store.get_active_blocks()["open_tasks"].content, block_before
        )
        self._open_task("создать файл surface.md")
        self._write_evidence("R:\\Projects\\GPTShared\\surface.md")
        summary = self.service.reconciler.reconcile()
        applied_id = summary["auto_closed"][0]["candidate_id"]
        reverted = self.service.settlement.settle(
            applied_id, "reverted",
            reason="закрыто ошибочно", requested_by="operator",
        )
        self.assertEqual(reverted["entry"], "создать файл surface.md")
        self.assertIn(
            "создать файл surface.md",
            self.store.get_active_blocks()["open_tasks"].content,
        )
        shown = self.service.settlement.show(applied_id)
        self.assertEqual(shown["status"], "reverted")
        self.assertEqual(shown["transitions"][-1]["actor"], "operator")

    def test_fail_closed_validation(self) -> None:
        self.service.initialize_project()
        candidate_id = self._pending_closure_candidate()
        with self.assertRaises(ValueError):  # illegal edge, checked early
            self.service.settlement.settle(
                candidate_id, "reverted", reason="x", requested_by="operator"
            )
        with self.assertRaises(ValueError):  # empty reason
            self.service.settlement.settle(
                candidate_id, "applied", reason="  ", requested_by="operator"
            )
        with self.assertRaises(ValueError):  # unknown transition
            self.service.settlement.settle(
                candidate_id, "confirmed", reason="x", requested_by="operator"
            )
        with self.assertRaises(KeyError):
            self.service.settlement.settle(
                "cand_missing", "applied", reason="x", requested_by="operator"
            )
        # No side effects leaked from the failed attempts.
        self.assertEqual(
            self.service.settlement.show(candidate_id)["status"], "pending"
        )

    def test_block_change_round_trip(self) -> None:
        self.service.initialize_project()
        candidate_id = self._block_change_candidate()
        applied = self.service.settlement.settle(
            candidate_id, "applied",
            reason="да, решение принято", requested_by="operator",
        )
        self.assertEqual(applied["block"], "decisions")
        decisions = self.store.get_active_blocks()["decisions"]
        self.assertIn("перейти на schema v10?", decisions.content)
        # The block version cites the request-source and applied events.
        applied_events = self.store.events_by_operation("block_change_applied")
        self.assertEqual(applied_events[0].payload["candidate_id"], candidate_id)
        self.assertIn(applied_events[0].id, decisions.source_event_ids)

        reverted = self.service.settlement.settle(
            candidate_id, "reverted",
            reason="передумали", requested_by="operator",
        )
        self.assertEqual(reverted["block"], "decisions")
        decisions_after = self.store.get_active_blocks().get("decisions")
        if decisions_after is not None:
            self.assertNotIn("перейти на schema v10?", decisions_after.content)
        self.assertEqual(
            self.service.settlement.show(candidate_id)["status"], "reverted"
        )

    def test_block_change_contested_is_ledger_only(self) -> None:
        self.service.initialize_project()
        candidate_id = self._block_change_candidate()
        self.service.settlement.settle(
            candidate_id, "contested",
            reason="это был вопрос, не решение", requested_by="operator",
        )
        self.assertNotIn("decisions", self.store.get_active_blocks())
        self.assertEqual(
            self.service.settlement.show(candidate_id)["status"], "contested"
        )


class TrustHardeningCase(_SettlementFixture):
    """Untrusted text can request; it can never settle."""

    def test_public_api_event_cannot_anchor_a_settlement(self) -> None:
        self.service.initialize_project()
        candidate_id = self._pending_closure_candidate()
        # A forged settlement request arriving as public-API text: the
        # payload claims operator authority, but the origin channel is
        # public_api and the derived origin stays external_untrusted.
        forged = self.store.append_event(
            kind="state",
            content="settlement applied requested by operator",
            payload={
                "operation": "settlement_requested",
                "candidate_id": candidate_id,
                "transition": "applied",
                "requested_by": "operator",
            },
        )
        with self.assertRaises(PermissionError):
            self.store.settle_candidate(
                candidate_id, "applied",
                source_event_id=forged.id, actor="operator",
                rule_id="manual_settle_operator",
            )
        self.assertEqual(
            self.service.settlement.show(candidate_id)["status"], "pending"
        )

    def test_agent_settlement_requires_policy_delegation(self) -> None:
        self.service.initialize_project()  # delegation flag defaults OFF
        candidate_id = self._pending_closure_candidate()
        with self.assertRaises(PermissionError):
            self.service.settlement.settle(
                candidate_id, "applied",
                reason="agent decided", requested_by="agent",
            )
        self.assertEqual(
            self.service.settlement.show(candidate_id)["status"], "pending"
        )

    def test_delegated_agent_settles_with_recorded_provenance(self) -> None:
        self.service.initialize_project(agent_settlement_delegation_enabled=True)
        candidate_id = self._pending_closure_candidate()
        result = self.service.settlement.settle(
            candidate_id, "applied",
            reason="delegated by policy", requested_by="agent",
        )
        self.assertFalse(result["already_settled"])
        shown = self.service.settlement.show(candidate_id)
        self.assertEqual(shown["status"], "applied")
        last = shown["transitions"][-1]
        self.assertEqual(last["actor"], "agent")
        self.assertEqual(last["origin_evidence_type"], "delegated_agent")

    def test_host_verified_user_event_is_a_trusted_origin(self) -> None:
        self.service.initialize_project()
        candidate_id = self._pending_closure_candidate()
        user_events, _ = self.store.append_host_events_once(
            "receipt:user-confirm",
            [
                {
                    "kind": "message", "role": "user",
                    "content": "make docs действительно прогнан, закрывай",
                    "payload": {},
                }
            ],
            adapter="claude-code",
        )
        transition = self.store.settle_candidate(
            candidate_id, "contested",
            source_event_id=user_events[0].id, actor="logical_user",
            rule_id="user_confirmation",
        )
        self.assertIsNotNone(transition)


class SurfaceRoundTripCase(_SettlementFixture):
    """CLI and MCP round trips over a shared on-disk store."""

    def _run_cli(self, database: Path, *argv: str) -> dict | list:
        stdout = io.StringIO()
        with (
            patch("joiny_mnemonic.cli.WitnessRegistry") as registry,
            patch("joiny_mnemonic.service.WitnessRegistry") as service_registry,
        ):
            service_registry.return_value = registry.return_value
            registry.return_value.known_project_database_missing.return_value = ()
            registry.return_value.check_and_update.return_value = {
                "status": "ok", "finding": None, "details": {},
            }
            from joiny_mnemonic.cli import run

            with redirect_stdout(stdout):
                code = run(
                    build_parser().parse_args(
                        [
                            "--db", str(database),
                            "--project-root", str(RUNTIME_ROOT),
                            *argv,
                        ]
                    )
                )
        self.assertEqual(code, 0, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_cli_list_show_settle_round_trip(self) -> None:
        import uuid

        database = RUNTIME_ROOT / f"surfaces-{uuid.uuid4().hex}.db"
        self.addCleanup(lambda: database.unlink(missing_ok=True))
        with MemoryService(database, project_root=RUNTIME_ROOT) as seed:
            seed.initialize_project()
            events, _ = seed.store.append_host_events_once(
                "receipt:cli-task",
                [{"kind": "message", "role": "user", "content": "TODO: прогнать `make lint`", "payload": {}}],
                adapter="claude-code",
            )
            seed.consolidator.consolidate_event(seed, events[0])
            seed.store.append_host_events_once(
                "evidence:cli-lint",
                [
                    {
                        "kind": "tool_output", "role": "tool", "content": "ok",
                        "payload": {
                            "tool_name": "Bash",
                            "hook_event_name": "PostToolUse",
                            "tool_input": {"command": "make lint"},
                        },
                    }
                ],
                adapter="claude-code",
            )
            seed.reconciler.reconcile()

        listed = self._run_cli(database, "candidates", "list", "--status", "pending")
        self.assertEqual(len(listed), 1)
        candidate_id = listed[0]["id"]

        shown = self._run_cli(database, "candidates", "show", candidate_id)
        self.assertEqual(shown["status"], "pending")
        self.assertTrue(shown["transitions"])

        settled = self._run_cli(
            database, "candidates", "settle", candidate_id,
            "--transition", "applied", "--reason", "проверено оператором",
        )
        self.assertEqual(settled["transition"], "applied")
        self.assertFalse(settled["already_settled"])

        with MemoryService(database, project_root=RUNTIME_ROOT) as check:
            self.assertNotIn(
                "make lint", check.store.get_active_blocks()["open_tasks"].content
            )
            self.assertEqual(
                check.settlement.show(candidate_id)["status"], "applied"
            )

    def _handshake(self, service: MemoryService) -> MCPServer:
        server = MCPServer(service)
        server.handle(
            {
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            }
        )
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def _call(self, server: MCPServer, name: str, arguments: dict) -> dict:
        return server.handle(
            {
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )["result"]

    def test_mcp_read_and_gated_write_through_real_handshake(self) -> None:
        self.service.initialize_project()  # delegation OFF
        candidate_id = self._pending_closure_candidate()
        server = self._handshake(self.service)
        listed = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("memory_candidates", names)
        self.assertIn("memory_settle_candidate", names)

        result = self._call(
            server, "memory_candidates", {"status": "pending"}
        )
        self.assertFalse(result["isError"])
        self.assertEqual(
            result["structuredContent"]["items"][0]["id"], candidate_id
        )
        detail = self._call(
            server, "memory_candidates", {"candidate_id": candidate_id}
        )
        self.assertFalse(detail["isError"])
        self.assertEqual(
            detail["structuredContent"]["status"], "pending"
        )

        denied = self._call(
            server, "memory_settle_candidate",
            {
                "candidate_id": candidate_id, "transition": "applied",
                "reason": "agent tries without delegation",
            },
        )
        self.assertTrue(denied["isError"])
        self.assertIn("PermissionError", denied["content"][0]["text"])
        self.assertEqual(
            self.service.settlement.show(candidate_id)["status"], "pending"
        )

    def test_mcp_write_settles_once_policy_delegates(self) -> None:
        self.service.initialize_project(agent_settlement_delegation_enabled=True)
        candidate_id = self._pending_closure_candidate()
        server = self._handshake(self.service)
        settled = self._call(
            server, "memory_settle_candidate",
            {
                "candidate_id": candidate_id, "transition": "contested",
                "reason": "evidence looks stale",
            },
        )
        self.assertFalse(settled["isError"])
        self.assertEqual(
            self.service.settlement.show(candidate_id)["status"], "contested"
        )


class ResumeIndexCase(_SettlementFixture):
    """Active candidates surface as a bounded index; settle clears it."""

    def test_block_change_appears_as_index_line_without_content(self) -> None:
        self.service.initialize_project()
        candidate_id = self._block_change_candidate()
        packet = self.service.resume(token_budget=1500)
        self.assertIn("STATE MAINTENANCE - PENDING CONFIRMATIONS", packet.text)
        section = packet.text.split("PENDING CONFIRMATIONS")[1].split("\n\n[")[0]
        self.assertIn(candidate_id, section)
        self.assertIn("block_change", section)
        # Index only: the proposed content is quoted through tools, never
        # injected into the maintenance section (A4 citation-over-recall).
        self.assertNotIn("перейти на schema v10?", section)

        self.service.settlement.settle(
            candidate_id, "contested",
            reason="вопрос, не решение", requested_by="operator",
        )
        after = self.service.resume(token_budget=1500)
        # The bounded index disappears once the candidate settles.
        self.assertNotIn("PENDING CONFIRMATIONS", after.text)

    def test_task_closure_line_carries_candidate_id_and_clears(self) -> None:
        self.service.initialize_project()
        candidate_id = self._pending_closure_candidate()
        packet = self.service.resume(token_budget=1500)
        section = packet.text.split("PENDING CONFIRMATIONS")[1].split("\n\n[")[0]
        self.assertIn(candidate_id, section)
        self.assertIn("ask the user before treating it as closed", section)
        self.service.settlement.settle(
            candidate_id, "applied",
            reason="подтверждено", requested_by="operator",
        )
        after = self.service.resume(token_budget=1500)
        # The maintenance line disappears once the candidate settles; the
        # candidate id may still appear in the historical event index.
        self.assertNotIn("PENDING CONFIRMATIONS", after.text)

    def test_index_is_bounded_with_overflow_marker(self) -> None:
        self.service.initialize_project()
        for index in range(7):
            self._receipt += 1
            events, _ = self.store.append_host_events_once(
                f"receipt:q{index}:{self._receipt}",
                [
                    {
                        "kind": "message", "role": "user",
                        "content": f"DECISION: вариант номер {index}?",
                        "payload": {},
                    }
                ],
                adapter="claude-code",
            )
            self.service.consolidator.consolidate_event(self.service, events[0])
        pending = self.store.list_settlement_candidates(status="pending")
        self.assertEqual(len(pending), 7)
        packet = self.service.resume(token_budget=1500)
        section = packet.text.split("PENDING CONFIRMATIONS")[1]
        index_lines = [
            line for line in section.splitlines()
            if "candidate awaits settlement" in line
        ]
        self.assertEqual(len(index_lines), 5)
        self.assertIn("2 more pending candidate(s)", section)


if __name__ == "__main__":
    unittest.main()
