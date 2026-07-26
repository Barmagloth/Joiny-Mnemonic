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
    def test_unknown_direct_store_call_is_a_violation(self) -> None:
        violations = calls_in_source(
            "def route(service):\n    service.store.future_write(value=1)\n",
            path="fixture.py",
        )
        self.assertEqual(
            [(item.path, item.line, item.method) for item in violations],
            [("fixture.py", 2, "future_write")],
        )

    def test_effective_store_read_marker_is_the_only_exception(self) -> None:
        reads = declared_store_reads(ROOT)
        self.assertIn("get_event", reads)
        self.assertNotIn("start_session", reads)
        source = "def route(service):\n    service.store.get_event('evt')\n"
        self.assertEqual(
            calls_in_source(source, path="fixture.py", read_methods=reads), ()
        )

    def test_store_capability_escape_is_a_violation(self) -> None:
        violations = calls_in_source(
            "def route(service):\n    consume(service.store)\n",
            path="fixture.py",
        )
        self.assertEqual(violations[0].method, "<store_escape>")

    def test_current_direct_store_write_inventory_is_frozen(self) -> None:
        violations = direct_store_writes(ROOT)
        self.assertEqual(
            [(item.path, item.line, item.method) for item in violations],
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


if __name__ == "__main__":
    unittest.main()
