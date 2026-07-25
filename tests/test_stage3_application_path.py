from __future__ import annotations

import unittest
from pathlib import Path

from scripts.stage3_surface_audit import (
    calls_in_source,
    declared_store_reads,
    direct_store_writes,
)


ROOT = Path(__file__).resolve().parents[1]


class Stage3SurfaceAuditTest(unittest.TestCase):
    def test_unknown_direct_store_call_fails_closed(self) -> None:
        writes = calls_in_source(
            "def route(service):\n    service.store.future_write(value=1)\n",
            path="fixture.py",
        )
        self.assertEqual(
            [(item.path, item.method) for item in writes],
            [("fixture.py", "future_write")],
        )

    def test_alias_bound_method_and_dynamic_access_fail_closed(self) -> None:
        fixtures = (
            "def route(service):\n    s = service.store\n    s.future_write()\n",
            "def route(service):\n    write = service.store.future_write\n    write()\n",
            "def route(service):\n    getattr(service.store, 'future_write')()\n",
            "def route(service):\n    getattr(service, 'store').future_write()\n",
            "def route(service, name):\n    getattr(service, name).future_write()\n",
            "def route(service):\n    service.__getattribute__('store').future_write()\n",
            "def route(service):\n    vars(service)['store'].future_write()\n",
            "def route(service):\n    service.__dict__['store'].future_write()\n",
            "def route(service):\n    service.__dict__.get('store').future_write()\n",
            "def route(service):\n    service.store['future_write']()\n",
            "def route(service, other):\n    service.store.path = other\n",
            "def route(service):\n    service.store.path.unlink()\n",
            "def route(service):\n    consume(service.store)\n",
        )
        for source in fixtures:
            with self.subTest(source=source):
                self.assertTrue(calls_in_source(source, path="fixture.py"))

    def test_read_classification_comes_from_store_declarations(self) -> None:
        reads = declared_store_reads(ROOT)
        self.assertEqual(reads, frozenset({
            "assert_integrity", "get_active_blocks", "get_artifact", "get_event",
            "list_extraction_candidates", "list_memories", "list_security_findings",
            "list_settlement_candidates", "list_tool_output_views",
            "task_for_hook_session",
        }))
        self.assertEqual(
            calls_in_source(
                "def route(service):\n    service.store.get_event('evt')\n",
                path="fixture.py",
                read_methods=reads,
            ),
            (),
        )
    def test_current_direct_store_write_inventory_is_frozen(self) -> None:
        writes = direct_store_writes(ROOT)
        self.assertEqual(
            [(item.path, item.line, item.method) for item in writes],
            [
                ("src/joiny_mnemonic/api.py", 177, "start_session"),
                ("src/joiny_mnemonic/api.py", 185, "create_branch"),
                ("src/joiny_mnemonic/api.py", 197, "append_artifact"),
                ("src/joiny_mnemonic/api.py", 199, "set_active_block"),
                ("src/joiny_mnemonic/api.py", 238, "set_budget_policy"),
                ("src/joiny_mnemonic/cli.py", 679, "record_security_finding"),
                ("src/joiny_mnemonic/cli.py", 689, "start_session"),
                ("src/joiny_mnemonic/cli.py", 691, "create_branch"),
                ("src/joiny_mnemonic/cli.py", 696, "append_artifact"),
                ("src/joiny_mnemonic/cli.py", 698, "set_active_block"),
                ("src/joiny_mnemonic/cli.py", 852, "set_budget_policy"),
                ("src/joiny_mnemonic/hooks.py", 376, "hook_session"),
                ("src/joiny_mnemonic/hooks.py", 383, "bind_task_session"),
                ("src/joiny_mnemonic/hooks.py", 421, "append_host_events_once"),
                ("src/joiny_mnemonic/hooks.py", 496, "after_commit"),
                ("src/joiny_mnemonic/mcp.py", 500, "set_active_block"),
            ],
        )
