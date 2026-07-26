from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic.api import make_handler
from joiny_mnemonic.cli import build_parser, run
from joiny_mnemonic.hooks import process_hook
from joiny_mnemonic.mcp import MCPServer, PROTOCOL_VERSION
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.storage import MemoryStore
from scripts.stage3_surface_audit import direct_store_writes


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ApplicationCommandsTest(unittest.TestCase):
    def _run_cli(self, database: Path, content: str) -> dict:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = run(
                build_parser().parse_args(
                    [
                        "--db",
                        str(database),
                        "--project-root",
                        str(RUNTIME_ROOT),
                        "block-set",
                        "goal",
                        content,
                    ]
                )
            )
        self.assertEqual(code, 0)
        return json.loads(stdout.getvalue())

    def test_commands_preserve_existing_storage_contracts(self) -> None:
        with MemoryService(":memory:", project_root=RUNTIME_ROOT) as service:
            branch_id = service.commands.create_branch("command-branch")
            session_id = service.commands.start_session(
                "codex", branch_id=branch_id, capabilities={"hooks": True}
            )
            artifact = service.commands.append_artifact(
                name="proof.txt",
                data="proof",
                branch_id=branch_id,
                session_id=session_id,
            )
            block = service.commands.set_active_block(
                "goal", "ship", branch_id=branch_id, session_id=session_id
            )
            policy = service.commands.set_budget_policy(
                branch_id=branch_id, context_window_tokens=1000
            )
            finding_id = service.commands.record_security_finding(
                "test_finding",
                incident_key="test_finding:application-command",
                details={"source": "test"},
            )

            self.assertEqual(branch_id, "command-branch")
            self.assertTrue(session_id.startswith("ses_"))
            self.assertEqual(artifact.name, "proof.txt")
            self.assertEqual(block.content, "ship")
            self.assertEqual(policy.context_window_tokens, 1000)
            self.assertTrue(finding_id.startswith("finding_"))

    def test_jm_inv_003_all_public_surfaces_share_application_path(self) -> None:
        database = RUNTIME_ROOT / f"stage3-command-{uuid.uuid4().hex}.db"
        self.addCleanup(lambda: database.unlink(missing_ok=True))
        oversized = "x" * (MemoryStore.MAX_ACTIVE_BYTES + 1)
        cli_result = self._run_cli(database, "ship")

        with MemoryService(":memory:", project_root=RUNTIME_ROOT) as service:
            mcp = MCPServer(service)
            initialized = mcp.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION},
                }
            )
            self.assertEqual(
                initialized["result"]["protocolVersion"], PROTOCOL_VERSION
            )
            self.assertIsNone(
                mcp.handle(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
            )
            mcp_success = mcp.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "memory_set_block",
                        "arguments": {"name": "goal", "content": "ship"},
                    },
                }
            )
            mcp_denied = mcp.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "memory_set_block",
                        "arguments": {"name": "goal", "content": oversized},
                    },
                }
            )
            self.assertFalse(mcp_success["result"]["isError"])
            self.assertTrue(mcp_denied["result"]["isError"])
            self.assertIn("ValueError", mcp_denied["result"]["content"][0]["text"])
            json.dumps(mcp_success)
            json.dumps(mcp_denied)
            mcp_result = mcp_success["result"]["structuredContent"]

        with MemoryService(":memory:", project_root=RUNTIME_ROOT) as service:
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def post(content: str) -> dict:
                request = urllib.request.Request(
                    base + "/v1/blocks",
                    data=json.dumps({"name": "goal", "content": content}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)

            try:
                http_result = post("ship")
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    post(oversized)
                self.assertEqual(denied.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        for result in (cli_result, mcp_result, http_result):
            self.assertEqual(
                {"name": result["name"], "content": result["content"]},
                {"name": "goal", "content": "ship"},
            )

        with self.assertRaises(ValueError):
            self._run_cli(database, oversized)

        with MemoryService(":memory:", project_root=RUNTIME_ROOT) as service:
            with (
                patch.object(
                    service.commands,
                    "resolve_hook_session",
                    wraps=service.commands.resolve_hook_session,
                ) as resolve_session,
                patch.object(
                    service.commands,
                    "append_host_events_once",
                    wraps=service.commands.append_host_events_once,
                ) as append_events,
                patch.object(
                    service.commands,
                    "schedule_after_commit",
                    wraps=service.commands.schedule_after_commit,
                ) as after_commit,
            ):
                result = process_hook(
                    service,
                    "codex",
                    {
                        "hook_event_name": "PostToolUse",
                        "session_id": "stage3-application-hook",
                        "task_id": "stage3-application-task",
                        "tool_use_id": "stage3-call",
                        "tool_name": "Read",
                        "tool_input": {"file_path": "README.md"},
                        "tool_response": {"content": "ok"},
                    },
                )

            self.assertEqual(result, {})
            resolve_session.assert_called_once()
            self.assertEqual(
                resolve_session.call_args.kwargs["task_key"],
                "stage3-application-task",
            )
            append_events.assert_called_once()
            after_commit.assert_called_once()

        self.assertEqual(direct_store_writes(PROJECT_ROOT), ())


if __name__ == "__main__":
    unittest.main()
