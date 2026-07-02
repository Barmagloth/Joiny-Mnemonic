from __future__ import annotations

import io
import json
import threading
import tomllib
import unittest
import urllib.request
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path

from joiny_mnemonic.adapters import ADAPTERS, adapter_capabilities
from joiny_mnemonic.api import make_handler
from joiny_mnemonic.mcp import MCPServer, PROTOCOL_VERSION, serve_stdio
from joiny_mnemonic.plugins import PluginRegistry
from joiny_mnemonic.physical import (
    PhysicalCandidate,
    PhysicalMemoryGovernor,
    Placement,
)
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class IntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)

    def tearDown(self) -> None:
        self.service.close()

    def test_distribution_and_console_script_identity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["project"]["name"], "joiny-mnemonic")
        self.assertEqual(
            config["project"]["scripts"],
            {
                "joiny-mnemonic": "joiny_mnemonic.cli:main",
                "joiny-mnemonic-benchmark": "joiny_mnemonic.benchmark_cli:main",
            },
        )
        self.assertEqual(
            config["tool"]["setuptools"]["packages"]["find"]["include"],
            ["joiny_mnemonic*"],
        )
    def test_renamed_plugin_groups_override_legacy_groups(self) -> None:
        with patch("joiny_mnemonic.plugins.entry_points", return_value=()) as points:
            PluginRegistry(load_installed=True)
        groups = [call.kwargs["group"] for call in points.call_args_list]
        self.assertEqual(
            groups,
            [
                "llm_memory.semantic",
                "llm_memory.knowledge_graph",
                "llm_memory.kv_tier",
                "joiny_mnemonic.semantic",
                "joiny_mnemonic.knowledge_graph",
                "joiny_mnemonic.kv_tier",
            ],
        )
    def test_four_agent_families_use_the_same_core(self) -> None:
        native = {
            "claude-code": {"hook_event_name": "UserPromptSubmit", "prompt": "from claude"},
            "codex": {"type": "user_message", "content": "from codex"},
            "opencode": {"type": "message", "content": "from opencode", "role": "user"},
            "openhands": {"type": "message", "content": "from openhands", "role": "user"},
        }
        for name, event in native.items():
            result = self.service.ingest_native(name, event)
            self.assertIsNotNone(result)
            self.assertTrue(adapter_capabilities(name)["text_memory"])
        self.assertEqual(len(ADAPTERS), 4)
        self.assertEqual(len(self.service.store.query_events()), 4)

    def test_capabilities_disable_unavailable_kv_without_disabling_text(self) -> None:
        values = adapter_capabilities("minimal-agent", {"hooks": [], "kv_access": False})
        self.assertFalse(values["event_ingestion"])
        self.assertFalse(values["kv_cache_tiers"])
        self.assertTrue(values["text_memory"])
        self.assertTrue(values["manual_cli_api_mcp"])
        self.assertEqual(
            self.service.capabilities()["core"]["durable_memory_markers"],
            ["Goal", "Decision", "Fact", "Constraint", "TODO", "Preference"],
        )

    def test_mcp_lifecycle_tools_and_calls(self) -> None:
        server = MCPServer(self.service)
        initialized = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            }
        )
        self.assertEqual(initialized["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertIsNone(
            server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("memory_append", {item["name"] for item in tools["result"]["tools"]})
        called = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "memory_append", "arguments": {"kind": "message", "content": "via MCP"}},
            }
        )
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(self.service.store.query_events()[-1].content, "via MCP")

    def test_stdio_is_newline_delimited_json_only(self) -> None:
        incoming = io.BytesIO(
            (
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
                + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            ).encode()
        )
        outgoing = io.BytesIO()
        serve_stdio(self.service, incoming, outgoing)
        lines = outgoing.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["result"], {})

    def test_mcp_exposes_usage_governor_and_task_boundaries(self) -> None:
        server = MCPServer(self.service)
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        })
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names = {item["name"] for item in server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )["result"]["tools"]}
        self.assertTrue({
            "memory_output_views", "memory_usage", "memory_governor",
            "memory_task_start", "memory_task_status", "memory_task_resume",
        }.issubset(names))
        task = server._call_tool(
            "memory_task_start",
            {"task_key": "MCP-1", "title": "MCP task boundary"},
        )
        self.assertTrue(task.branch_id.startswith("task/mcp-1-"))
        packet = server._call_tool(
            "memory_task_resume", {"task_key": "MCP-1", "token_budget": 600}
        )
        self.assertLessEqual(packet.estimated_tokens, 600)
        usage = server._call_tool("memory_usage", {"branch_id": task.branch_id})
        self.assertIn("totals", usage)

    def test_physical_memory_governor_compares_store_and_recompute(self) -> None:
        candidates = [
            PhysicalCandidate(Placement.TEXT_RECOMPUTE, 0, 0, 100, 5),
            PhysicalCandidate(Placement.GPU_KV, 2_000, 2, 100, 5),
            PhysicalCandidate(Placement.CPU_QUANTIZED_KV, 500, 15, 100, 5),
        ]
        decision = PhysicalMemoryGovernor().choose(candidates, memory_budget_bytes=1_000)
        self.assertEqual(decision.placement, Placement.CPU_QUANTIZED_KV)

    def test_http_exposes_task_and_usage_endpoints(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"task_key": "HTTP-1", "title": "HTTP task"}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/tasks",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                task = json.load(response)
            self.assertTrue(task["branch_id"].startswith("task/http-1-"))
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/v1/tasks", timeout=5
            ) as response:
                tasks = json.load(response)
            self.assertEqual(tasks[0]["task_key"], "HTTP-1")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/v1/usage?branch={task['branch_id']}",
                timeout=5,
            ) as response:
                usage = json.load(response)
            self.assertIn("totals", usage)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_api_uses_the_same_store(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"kind": "message", "content": "via HTTP"}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/events",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
            self.assertEqual(result["content"], "via HTTP")
            self.assertEqual(self.service.store.query_events()[-1].content, "via HTTP")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
