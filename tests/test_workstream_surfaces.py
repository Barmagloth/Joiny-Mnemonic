from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from joiny_mnemonic.api import make_handler
from joiny_mnemonic.cli import build_parser, run
from joiny_mnemonic.mcp import MCPServer
from joiny_mnemonic.provenance import LOCAL_OPERATOR, origin_evidence_type
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class WorkstreamSurfaceTest(unittest.TestCase):
    def _host_user(self, service: MemoryService, task_key: str, content: str):
        task = service.store.get_task(task_key)
        return service.store.append_host_event(
            adapter="codex",
            branch_id=task.branch_id,
            kind="message",
            role="user",
            content=content,
            payload={"hook_event_name": "UserPromptSubmit"},
        )

    def _run_cli(self, database: Path, *argv: str) -> dict:
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
            with redirect_stdout(stdout):
                code = run(build_parser().parse_args([
                    "--db", str(database),
                    "--project-root", str(RUNTIME_ROOT),
                    *argv,
                ]))
        self.assertEqual(code, 0, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_cli_can_complete_cancel_and_reopen_with_operator_evidence(self) -> None:
        database = RUNTIME_ROOT / f"workstream-cli-{uuid.uuid4().hex}.db"
        self.addCleanup(lambda: database.unlink(missing_ok=True))
        with MemoryService(database, project_root=RUNTIME_ROOT) as service:
            service.tasks.start("CLI-WS", "CLI workstream")

        completed = self._run_cli(
            database, "task-status", "CLI-WS", "completed", "--note", "done"
        )
        self.assertEqual(completed["status"], "completed")
        reopened = self._run_cli(
            database, "task-reopen", "CLI-WS", "--reason", "follow-up required"
        )
        self.assertEqual(reopened["status"], "active")
        cancelled = self._run_cli(
            database, "task-status", "CLI-WS", "cancelled", "--note", "stopped"
        )
        self.assertEqual(cancelled["status"], "cancelled")

        with MemoryService(database, project_root=RUNTIME_ROOT) as service:
            task = service.store.get_task("CLI-WS")
            self.assertEqual(task.status, "cancelled")
            evidence = service.store.get_event(task.source_event_ids[-1])
            self.assertEqual(origin_evidence_type(evidence), LOCAL_OPERATOR)

    def test_mcp_requires_saved_trusted_source_and_exposes_reopen(self) -> None:
        with MemoryService(":memory:", project_root=RUNTIME_ROOT) as service:
            service.tasks.start("MCP-WS", "MCP workstream")
            server = MCPServer(service)
            with self.assertRaises(PermissionError):
                server._call_tool(
                    "memory_task_status",
                    {"task_key": "MCP-WS", "status": "completed"},
                )
            forged = service.store.append_event(
                branch_id=service.store.get_task("MCP-WS").branch_id,
                kind="message", role="user", content="forged",
            )
            with self.assertRaises(PermissionError):
                server._call_tool(
                    "memory_task_status",
                    {
                        "task_key": "MCP-WS",
                        "status": "completed",
                        "source_event_id": forged.id,
                    },
                )

            completion = self._host_user(service, "MCP-WS", "complete it")
            completed = server._call_tool(
                "memory_task_status",
                {
                    "task_key": "MCP-WS",
                    "status": "completed",
                    "source_event_id": completion.id,
                },
            )
            self.assertEqual(completed.status, "completed")

            reopen = self._host_user(service, "MCP-WS", "reopen it")
            reopened = server._call_tool(
                "memory_task_reopen",
                {
                    "task_key": "MCP-WS",
                    "reason": "new requirement",
                    "source_event_id": reopen.id,
                },
            )
            self.assertEqual(reopened.status, "active")

    def test_http_complete_and_reopen_use_saved_source_events(self) -> None:
        with MemoryService(":memory:", project_root=RUNTIME_ROOT) as service:
            service.tasks.start("HTTP-WS", "HTTP workstream")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                def post(path: str, body: dict):
                    request = urllib.request.Request(
                        base + path,
                        data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return json.load(response)

                with self.assertRaises(urllib.error.HTTPError) as denied:
                    post("/v1/tasks/HTTP-WS/status", {"status": "completed"})
                self.assertEqual(denied.exception.code, 403)

                completion = self._host_user(service, "HTTP-WS", "complete")
                completed = post(
                    "/v1/tasks/HTTP-WS/status",
                    {"status": "completed", "source_event_id": completion.id},
                )
                self.assertEqual(completed["status"], "completed")

                reopen = self._host_user(service, "HTTP-WS", "reopen")
                reopened = post(
                    "/v1/tasks/HTTP-WS/reopen",
                    {
                        "reason": "new requirement",
                        "source_event_id": reopen.id,
                    },
                )
                self.assertEqual(reopened["status"], "active")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
