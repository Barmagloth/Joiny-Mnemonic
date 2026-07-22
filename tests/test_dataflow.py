from __future__ import annotations

import importlib.util
import json
import sqlite3
import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.plugins import PluginRegistry
from joiny_mnemonic.service import MemoryService


class DataflowTest(unittest.TestCase):
    def setUp(self) -> None:
        runtime = Path(__file__).resolve().parent / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.database = runtime / f"dataflow-{uuid.uuid4().hex}.db"
        self.service = MemoryService(
            self.database, project_root=runtime,
            plugins=PluginRegistry(load_installed=False),
        )

    def tearDown(self) -> None:
        self.service.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.database) + suffix).unlink(missing_ok=True)

    def test_append_records_exact_redacted_pipeline_and_is_immutable(self) -> None:
        event = self.service.append_event(
            kind="message",
            role="user",
            content="Decision: expose the flow",
            payload={"api_key": "not-for-the-ledger", "count": 42},
        )
        operations = self.service.store.list_dataflow_operations()
        self.assertEqual(len(operations), 1)
        operation = self.service.store.get_dataflow_operation(
            operations[0]["operation_id"]
        )
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(
            [entry["stage"] for entry in operation["entries"]],
            [
                "operation",
                "boundary.validation",
                "security.redaction",
                "persistence.canonical_append",
                "consolidation",
                "extraction.wakeup",
                "integrity.witness",
                "operation",
            ],
        )
        serialized = json.dumps(operation, ensure_ascii=False)
        self.assertNotIn("not-for-the-ledger", serialized)
        self.assertIn("[REDACTED]", serialized)
        persistence = operation["entries"][3]
        self.assertEqual(persistence["refs"]["event_id"], event.id)
        self.assertEqual(persistence["output"]["content"], event.content)

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.store._conn.execute(
                "UPDATE dataflow_entries SET status='failed' WHERE operation_id=?",
                (operation["operation_id"],),
            )

    def test_search_and_resume_explain_selection_and_final_packet(self) -> None:
        self.service.append_event(
            kind="message", role="user", content="Fact: violet engine uses SQLite"
        )
        hits = self.service.search(query="violet SQLite", semantic=False)
        self.assertTrue(hits)
        packet = self.service.resume(
            query="violet SQLite", parent_operation_id="op_parent"
        )
        self.assertIn("violet engine", packet.text)

        operations = self.service.store.list_dataflow_operations()
        by_name = {
            item["operation_name"]: self.service.store.get_dataflow_operation(
                item["operation_id"]
            )
            for item in operations
        }
        search_stages = [entry["stage"] for entry in by_name["search"]["entries"]]
        self.assertIn("retrieval.rank_and_filter", search_stages)
        resume_stages = [entry["stage"] for entry in by_name["resume"]["entries"]]
        self.assertEqual(by_name["resume"]["parent_operation_id"], "op_parent")
        self.assertIn("snapshot.restore_or_build", resume_stages)
        self.assertIn("prompt.active_memory", resume_stages)
        self.assertIn("prompt.recent_transcript", resume_stages)
        self.assertIn("prompt.retrieval", resume_stages)
        self.assertIn("prompt.packet", resume_stages)

    @unittest.skipUnless(
        importlib.util.find_spec("fastapi") and importlib.util.find_spec("pydantic"),
        "explorer extras are not installed",
    )
    def test_explorer_serves_ui_and_records_strict_validation_failure(self) -> None:
        from fastapi.testclient import TestClient
        from joiny_mnemonic.explorer import create_explorer_app

        client = TestClient(create_explorer_app(self.service))
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Joiny-Mnemonic " + chr(183) + " Потоки данных", response.text
        )
        self.assertIn("Автообновление", response.text)
        self.assertIn("Решение маршрутизации (почему)", response.text)
        self.assertIn(
            'function showEmpty(message="В этой ветке операций пока нет.")',
            response.text,
        )
        self.assertIn('if(!state.selected){showEmpty();return;}', response.text)
        self.assertIn('return "начало";', response.text)
        self.assertIn('button.setAttribute("aria-label"', response.text)
        self.assertNotIn(
            'button.setAttribute("role","listitem")', response.text
        )

        rejected = client.post(
            "/v1/events",
            json={"content": "bad boundary", "unexpected": "field"},
        )
        self.assertEqual(rejected.status_code, 400)
        operations = self.service.store.list_dataflow_operations()
        validation = next(
            item for item in operations if item["operation_name"] == "http_validation"
        )
        self.assertEqual(validation["status"], "failed")

        accepted = client.post(
            "/v1/events",
            json={
                "kind": "message", "role": "user", "content": "Fact: explorer is live",
                "branch_id": "main", "payload": {}, "files": [],
            },
        )
        self.assertEqual(accepted.status_code, 200)
        listed = client.get("/v1/dataflow/operations?branch=main").json()
        self.assertTrue(any(item["operation_name"] == "append_event" for item in listed))


if __name__ == "__main__":
    unittest.main()
