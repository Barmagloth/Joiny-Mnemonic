from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import tomllib
import unittest
import urllib.request
import uuid
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from joiny_mnemonic.adapters import ADAPTERS, adapter_capabilities
from joiny_mnemonic.api import make_handler
from joiny_mnemonic.hooks import install_hooks
from joiny_mnemonic.cli import build_parser
from joiny_mnemonic.mcp import MCPServer, PROTOCOL_VERSION, serve_stdio
from joiny_mnemonic.paths import resolve_runtime_database, resolve_runtime_project
from joiny_mnemonic.plugins import PluginContext, PluginRegistry
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

    def test_cli_survives_narrow_console_encoding(self) -> None:
        """First-live-run regression: memory content with characters outside
        the console codepage (e.g. "↔") crashed every CLI read command with
        'charmap' codec errors on Windows; output must degrade, not die."""
        root = RUNTIME_ROOT / f"narrow-console-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "cp1251",  # narrow codepage without "↔"
            }
        )
        base = [
            sys.executable, "-m", "joiny_mnemonic",
            "--db", str(root / "memory.db"), "--project-root", str(root),
        ]
        appended = subprocess.run(
            [*base, "append", "--kind", "message", "--role", "user",
             "--content", "GPT↔Claude bridge ⇄ проверка"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(appended.returncode, 0, appended.stderr)
        shown = subprocess.run(
            [*base, "timeline", "--limit", "5"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("Claude bridge", shown.stdout)
        self.assertNotIn("charmap", shown.stderr)

    def test_cli_init_resolves_witness_registry(self) -> None:
        root = RUNTIME_ROOT / f"cli-init-{uuid.uuid4().hex}"
        root.mkdir()
        try:
            with (
                patch("joiny_mnemonic.cli.WitnessRegistry") as registry,
                patch("joiny_mnemonic.service.WitnessRegistry") as service_registry,
            ):
                service_registry.return_value = registry.return_value
                registry.return_value.known_project_database_missing.return_value = ()
                registry.return_value.check_and_update.return_value = {
                    "status": "first_checkpoint", "finding": None, "details": {}
                }
                from joiny_mnemonic.cli import run

                result = run(
                    build_parser().parse_args(
                        [
                            "--db",
                            str(root / "memory.db"),
                            "--project-root",
                            str(root),
                            "init",
                        ]
                    )
                )
            self.assertEqual(result, 0)
        finally:
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                else:
                    path.rmdir()
            root.rmdir()

    def test_distribution_and_console_script_identity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["project"]["name"], "joiny-mnemonic")
        self.assertEqual(config["project"]["license"], {"file": "LICENSE"})
        self.assertTrue(
            (root / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
        )
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
        graph = build_parser().parse_args(["graph-neighbors", "SQLite"])
        self.assertEqual(graph.command, "graph-neighbors")
        self.assertEqual(graph.entity, "SQLite")
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
                "llm_memory.extractor",
                "joiny_mnemonic.semantic",
                "joiny_mnemonic.knowledge_graph",
                "joiny_mnemonic.kv_tier",
                "joiny_mnemonic.extractor",
            ],
        )
    def test_entry_point_factory_receives_project_context(self) -> None:
        context = PluginContext(
            project_root=RUNTIME_ROOT,
            database_path=RUNTIME_ROOT / "context.db",
        )

        class Point:
            name = "context-aware"

            @staticmethod
            def load():
                def factory(*, context):
                    return SimpleNamespace(name="context-aware", context=context)

                return factory

        def points(*, group):
            return (Point(),) if group == "joiny_mnemonic.semantic" else ()

        with patch("joiny_mnemonic.plugins.entry_points", side_effect=points):
            registry = PluginRegistry(context=context)
        self.assertIs(registry.semantic["context-aware"].context, context)
        self.assertEqual(registry.errors, [])
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
            [
                "Goal", "Decision", "Fact", "Constraint", "TODO", "Preference",
                "Failed", "Failure", "Lesson",
            ],
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
        names = {item["name"] for item in tools["result"]["tools"]}
        self.assertIn("memory_append", names)
        self.assertIn("memory_graph_neighbors", names)
        self.assertIn("memory_snapshot_prune", names)
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

    def test_claude_mcp_relative_paths_follow_claude_project_dir(self) -> None:
        root = RUNTIME_ROOT / f"claude-project-dir-{uuid.uuid4().hex}"
        project = root / "project"
        foreign_cwd = root / "launcher-cwd"
        (project / ".git").mkdir(parents=True)
        foreign_cwd.mkdir(parents=True)
        env = os.environ.copy()
        env.update(
            {
                "CLAUDE_PROJECT_DIR": str(project),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "claude-path-probe",
            "cwd": str(project),
            "prompt": "Decision: route-probe uses the project database",
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "joiny_mnemonic",
                "--db",
                ".joiny-mnemonic/memory.db",
                "--project-root",
                ".",
                "hook",
                "--agent",
                "claude-code",
            ],
            cwd=foreign_cwd,
            env=env,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        project_database = project / ".joiny-mnemonic" / "memory.db"
        self.assertTrue(project_database.exists())
        self.assertFalse((foreign_cwd / ".joiny-mnemonic" / "memory.db").exists())
        resolved_root = resolve_runtime_project(".", environ=env)
        resolved_database = resolve_runtime_database(
            ".joiny-mnemonic/memory.db", resolved_root
        )
        self.assertEqual(resolved_root, project.resolve())
        self.assertEqual(Path(resolved_database), project_database.resolve())
        service = MemoryService(resolved_database, project_root=resolved_root)
        try:
            self.assertIn(
                "route-probe uses the project database",
                {record.content for record in service.store.list_memories()},
            )
            capability = service.capabilities("claude-code")["agent"]
            self.assertTrue(capability["hook_database_matches"])
            self.assertEqual(
                Path(capability["active_database_path"]), project_database.resolve()
            )
        finally:
            service.close()

    def test_hook_cli_accepts_utf8_bom_from_powershell_pipe(self) -> None:
        root = RUNTIME_ROOT / f"hook-utf8-bom-{uuid.uuid4().hex}"
        project = root / "project"
        project.mkdir(parents=True)
        database = project / ".joiny-mnemonic" / "memory.db"
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        marker = "utf8-bom hook payload reached durable memory"
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "powershell-bom-probe",
            "cwd": str(project),
            "prompt": f"Fact: {marker}",
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "joiny_mnemonic",
                "--db",
                str(database),
                "--project-root",
                str(project),
                "hook",
                "--agent",
                "claude-code",
            ],
            cwd=project,
            env=env,
            input=b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        service = MemoryService(database, project_root=project)
        try:
            self.assertIn(
                f"Fact: {marker}",
                {event.content for event in service.store.query_events()},
            )
            self.assertIn(
                marker,
                {record.content for record in service.store.list_memories()},
            )
            self.assertTrue(service.store.has_hook_activity("claude-code"))
        finally:
            service.close()

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

    def test_capabilities_and_mcp_distinguish_installer_from_active_hooks(self) -> None:
        root = RUNTIME_ROOT / f"hook-status-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        global_path = root / "isolated-global" / "settings.json"
        service = MemoryService(":memory:", project_root=root)
        try:
            with patch(
                "joiny_mnemonic.hooks.resolve_global_install_path",
                return_value=global_path,
            ):
                missing = service.capabilities("claude-code")
                agent = missing["agent"]
                self.assertTrue(agent["hook_installer_available"])
                self.assertFalse(agent["hooks_configured"])
                self.assertFalse(agent["event_ingestion"])
                self.assertFalse(agent["hook_runtime_verified"])
                self.assertTrue(missing["warnings"])

                server = MCPServer(service)
                initialized = server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "clientInfo": {"name": "Claude Code", "version": "test"},
                        },
                    }
                )
                instructions = initialized["result"]["instructions"]
                self.assertIn("MCP alone does not capture", instructions)
                self.assertIn("NOT active", instructions)
                self.assertIn("install-hooks claude-code", instructions)

                install_hooks("claude-code", root)
                configured = service.capabilities("claude-code")["agent"]
                self.assertTrue(configured["hooks_configured"])
                self.assertFalse(configured["event_ingestion"])
                self.assertFalse(configured["hook_runtime_verified"])

                configured_server = MCPServer(service)
                configured_init = configured_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "clientInfo": {"name": "Claude Code", "version": "test"},
                        },
                    }
                )
                self.assertIn(
                    "no delivery has been observed",
                    configured_init["result"]["instructions"],
                )

                service.store.hook_session("claude-code", "observed-session")
                observed = service.capabilities("claude-code")["agent"]
                self.assertTrue(observed["hook_runtime_verified"])
                self.assertTrue(observed["event_ingestion"])
        finally:
            service.close()

    def test_mcp_reports_hook_database_split(self) -> None:
        root = RUNTIME_ROOT / f"hook-database-split-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        install_hooks("claude-code", root)
        service = MemoryService(root / "alternate.db", project_root=root)
        try:
            capabilities = service.capabilities("claude-code")
            agent = capabilities["agent"]
            self.assertFalse(agent["hook_database_matches"])
            self.assertTrue(
                any("automatic capture and MCP search are split" in warning
                    for warning in capabilities["warnings"])
            )
            initialized = MCPServer(service).handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "clientInfo": {"name": "Claude Code", "version": "test"},
                    },
                }
            )
            self.assertIn(
                "SPLIT across databases", initialized["result"]["instructions"]
            )
        finally:
            service.close()

    def test_capabilities_report_invalid_claude_settings(self) -> None:
        root = RUNTIME_ROOT / f"invalid-capability-{uuid.uuid4().hex}"
        settings = root / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"hooks": [] "broken": true}', encoding="utf-8")
        service = MemoryService(":memory:", project_root=root)
        try:
            with patch(
                "joiny_mnemonic.hooks.resolve_global_install_path",
                return_value=root / "isolated-global" / "settings.json",
            ):
                result = service.capabilities("claude-code")
            agent = result["agent"]
            self.assertEqual(agent["hook_configuration_status"], "invalid-config")
            self.assertFalse(agent["hook_config_valid"])
            self.assertFalse(agent["hooks_configured"])
            self.assertFalse(agent["event_ingestion"])
            self.assertIn(str(settings.resolve()), result["warnings"][0])
        finally:
            service.close()
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
            graph_body = json.dumps({"entity": "SQLite"}).encode()
            graph_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/graph/neighbors",
                data=graph_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(graph_request, timeout=5) as response:
                graph = json.load(response)
            self.assertEqual(graph, [])

            context_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/context",
                data=json.dumps({"id": result["id"], "before": 0, "after": 0}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(context_request, timeout=5) as response:
                context = json.load(response)
            self.assertEqual(context["index"][0]["id"], result["id"])

            source_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/source",
                data=json.dumps({"ids": [result["id"]]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(source_request, timeout=5) as response:
                sources = json.load(response)
            self.assertEqual(sources[0]["events"][0]["id"], result["id"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
