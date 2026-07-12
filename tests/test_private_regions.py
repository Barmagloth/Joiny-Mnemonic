from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from joiny_mnemonic.api import make_handler
from joiny_mnemonic.hooks import process_hook
from joiny_mnemonic.mcp import MCPServer
from joiny_mnemonic.security import Redaction, SecretRedactor
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"


class PrivateRegionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)

    def tearDown(self) -> None:
        self.service.close()

    def test_scanner_handles_regions_attributes_case_and_unclosed_input(self) -> None:
        redactor = SecretRedactor()
        cases = (
            (
                "before <private>secret</private> after",
                "before [PRIVATE CONTENT OMITTED] after",
                1,
            ),
            (
                "a<PrIvAtE reason='pii'>one</PRIVATE>b<private>two</private>c",
                "a[PRIVATE CONTENT OMITTED]b[PRIVATE CONTENT OMITTED]c",
                2,
            ),
            (
                "keep <private data-kind=\"secret\">remove to end",
                "keep [PRIVATE CONTENT OMITTED]",
                1,
            ),
            (
                "stray </private> stays",
                "stray </private> stays",
                0,
            ),
        )
        for source, expected, count in cases:
            with self.subTest(source=source):
                result, changes = redactor.redact_text(source)
                self.assertEqual(result, expected)
                self.assertEqual(
                    sum(item.count for item in changes if item.rule == "private_region"),
                    count,
                )

    def test_private_regions_precede_secret_rules_and_recurse(self) -> None:
        redactor = SecretRedactor()
        value = {
            "items": [
                "visible <private>Goal: leak sk-12345678901234567890</private>",
                {"note": "api_key=outside-secret"},
            ]
        }

        result, changes = redactor.redact_value(value)

        self.assertEqual(
            result["items"][0],
            "visible [PRIVATE CONTENT OMITTED]",
        )
        self.assertEqual(result["items"][1]["note"], "api_key=[REDACTED]")
        self.assertIn(Redaction("private_region", 1), changes)
        self.assertIn(Redaction("assigned_secret", 1), changes)

    def test_removed_content_is_absent_from_sqlite_and_only_count_is_persisted(self) -> None:
        root = RUNTIME_ROOT / f"private-db-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        database = root / "memory.db"
        hidden = "UNIQUE-PRIVATE-BYTES-71c345"
        service = MemoryService(database, project_root=root)
        event = service.store.append_event(
            kind="message",
            role="user",
            content=f"before <private>{hidden}</private> after",
            payload={"nested": [f"<PRIVATE reason='test'>{hidden}-payload</PRIVATE>"]},
        )
        service.close()

        self.assertEqual(event.content, "before [PRIVATE CONTENT OMITTED] after")
        self.assertEqual(
            event.payload["_security_redactions"]["private_regions_omitted"],
            2,
        )
        database_bytes = database.read_bytes()
        self.assertNotIn(hidden.encode(), database_bytes)
        self.assertNotIn(b"<private>", database_bytes.lower())

    def test_text_artifact_records_counts_and_binary_artifact_is_not_rewritten(self) -> None:
        hidden = "ARTIFACT-PRIVATE-983ac1"
        artifact = self.service.store.append_artifact(
            name="report.txt",
            data=f"before <private>{hidden}</private> after",
            mime_type="text/plain",
        )
        event = self.service.store.get_event(artifact.event_id)

        self.assertEqual(artifact.data, b"before [PRIVATE CONTENT OMITTED] after")
        self.assertEqual(
            event.payload["_security_redactions"]["private_regions_omitted"],
            1,
        )
        binary = b"prefix <private>opaque bytes</private> suffix"
        stored_binary = self.service.store.append_artifact(
            name="opaque.bin",
            data=binary,
            mime_type="application/octet-stream",
        )
        self.assertEqual(stored_binary.data, binary)
    def test_hook_cli_http_and_mcp_share_private_redaction(self) -> None:
        hidden = "SURFACE-PRIVATE-4ee321"
        process_hook(
            self.service,
            "codex",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "private-hook",
                "prompt": f"hook <private>{hidden}-hook</private>",
            },
        )
        mcp = MCPServer(self.service)
        mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
        mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "memory_append",
                    "arguments": {
                        "kind": "message",
                        "content": f"mcp <private>{hidden}-mcp</private>",
                    },
                },
            }
        )
        self.assertFalse(response["result"]["isError"])

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "kind": "message",
                    "content": f"http <private>{hidden}-http</private>",
                }
            ).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/events",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        cli_root = RUNTIME_ROOT / f"private-cli-{uuid.uuid4().hex}"
        cli_root.mkdir(parents=True)
        cli_db = cli_root / "memory.db"
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "joiny_mnemonic",
                "--db",
                str(cli_db),
                "--project-root",
                str(cli_root),
                "append",
                "--kind",
                "message",
                "--content",
                f"cli <private>{hidden}-cli</private>",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertNotIn(hidden, completed.stdout)

        contents = [event.content for event in self.service.store.query_events()]
        self.assertTrue(any("hook [PRIVATE CONTENT OMITTED]" == item for item in contents))
        self.assertTrue(any("mcp [PRIVATE CONTENT OMITTED]" == item for item in contents))
        self.assertTrue(any("http [PRIVATE CONTENT OMITTED]" == item for item in contents))
        self.assertNotIn(hidden.encode(), cli_db.read_bytes())


if __name__ == "__main__":
    unittest.main()