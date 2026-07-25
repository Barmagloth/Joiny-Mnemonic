from __future__ import annotations

import unittest
from pathlib import Path

from scripts.stage3_surface_audit import calls_in_source, direct_store_writes


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

    def test_current_direct_store_write_inventory_is_frozen(self) -> None:
        writes = direct_store_writes(ROOT)
        self.assertEqual(
            sorted((item.path, item.method) for item in writes),
            sorted({
                ("src/joiny_mnemonic/api.py", "append_artifact"),
                ("src/joiny_mnemonic/api.py", "create_branch"),
                ("src/joiny_mnemonic/api.py", "set_active_block"),
                ("src/joiny_mnemonic/api.py", "set_budget_policy"),
                ("src/joiny_mnemonic/api.py", "start_session"),
                ("src/joiny_mnemonic/cli.py", "append_artifact"),
                ("src/joiny_mnemonic/cli.py", "create_branch"),
                ("src/joiny_mnemonic/cli.py", "record_security_finding"),
                ("src/joiny_mnemonic/cli.py", "set_active_block"),
                ("src/joiny_mnemonic/cli.py", "set_budget_policy"),
                ("src/joiny_mnemonic/cli.py", "start_session"),
                ("src/joiny_mnemonic/hooks.py", "after_commit"),
                ("src/joiny_mnemonic/hooks.py", "append_host_events_once"),
                ("src/joiny_mnemonic/hooks.py", "bind_task_session"),
                ("src/joiny_mnemonic/hooks.py", "hook_session"),
                ("src/joiny_mnemonic/mcp.py", "set_active_block"),
            }),
        )
