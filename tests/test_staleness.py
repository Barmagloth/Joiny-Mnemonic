from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic.mcp import MCPServer
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
BASELINE = "2020-01-01T00:00:00+00:00"


class StalenessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_ROOT / f"staleness-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self._git("init")
        self._git("config", "user.email", "joiny@example.test")
        self._git("config", "user.name", "Joiny Test")
        self.file = self.root / "src" / "auth.py"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("version = 0\n", encoding="utf-8")
        self._git("add", "src/auth.py")
        self._git("commit", "-m", "initial")
        self.database = self.root / "memory.db"
        self.service = MemoryService(self.database, project_root=self.root)

    def tearDown(self) -> None:
        self.service.close()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def _commit(self, index: int) -> None:
        self.file.write_text(f"version = {index}\n", encoding="utf-8")
        self._git("add", "src/auth.py")
        self._git("commit", "-m", f"change {index}")

    def _memory(
        self,
        memory_type: str = "fact",
        content: str = "Auth behavior",
        file: str = "src/auth.py",
        *,
        branch_id: str = "main",
        supersedes_id: str | None = None,
        timestamp: str = BASELINE,
    ):
        with patch("joiny_mnemonic.storage._now", return_value=timestamp):
            source = self.service.store.append_event(
                kind="message",
                role="user",
                content=content,
                files=(file,),
                branch_id=branch_id,
            )
            return self.service.derive_memory(
                memory_type=memory_type,
                content=content,
                source_event_ids=(source.id,),
                files=(file,),
                branch_id=branch_id,
                supersedes_id=supersedes_id,
            )

    def test_current_threshold_crossing_and_search_ranking_is_unchanged(self) -> None:
        record = self._memory(content="Auth retry behavior")
        current = self.service.stale(memory_id=record.id)[0]
        self.assertEqual(current.status, "current")
        self.assertEqual(current.commits_since["src/auth.py"], 1)

        before = self.service.search(
            query="Auth retry",
            include_events=False,
            semantic=False,
        )
        self._commit(1)
        self._commit(2)
        stale = self.service.stale(memory_id=record.id)[0]
        self.assertEqual(stale.status, "possibly_stale")
        self.assertEqual(stale.commits_since["src/auth.py"], 3)

        after = self.service.search(
            query="Auth retry",
            include_events=False,
            semantic=False,
            include_staleness=True,
        )
        self.assertEqual(
            [(hit.id, hit.score) for hit in before],
            [(hit.id, hit.score) for hit in after],
        )
        self.assertEqual(after[0].metadata["staleness"]["status"], "possibly_stale")

    def test_missing_file_superseded_memory_and_branch_visibility(self) -> None:
        missing = self._memory(file="src/deleted.py", content="Deleted behavior")
        self.assertEqual(
            self.service.stale(memory_id=missing.id)[0].status,
            "missing_file",
        )

        old = self._memory(memory_type="lesson", content="Old lesson")
        replacement = self._memory(
            memory_type="lesson",
            content="Replacement lesson",
            supersedes_id=old.id,
        )
        visible_ids = {item.memory_id for item in self.service.stale()}
        self.assertNotIn(old.id, visible_ids)
        self.assertIn(replacement.id, visible_ids)

        self.service.store.create_branch("child")
        hidden = self._memory(memory_type="failure", content="Parent after fork")
        child_ids = {item.memory_id for item in self.service.stale(branch_id="child")}
        self.assertIn(replacement.id, child_ids)
        self.assertNotIn(hidden.id, child_ids)

    def test_git_calls_are_memoized_for_shared_file_and_baseline(self) -> None:
        first = self._memory(content="First shared memory")
        second = self._memory(content="Second shared memory")
        real_run = subprocess.run
        with patch("joiny_mnemonic.staleness.subprocess.run", wraps=real_run) as run:
            results = self.service.stale()

        self.assertTrue({first.id, second.id}.issubset({item.memory_id for item in results}))
        log_calls = [
            call
            for call in run.call_args_list
            if "log" in call.args[0]
        ]
        self.assertEqual(len(log_calls), 1)

    def test_git_unavailable_timeout_nonrepo_and_bad_timestamp_are_unknown(self) -> None:
        record = self._memory()
        with patch(
            "joiny_mnemonic.staleness.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            self.assertEqual(
                self.service.stale(memory_id=record.id)[0].status,
                "unknown",
            )
        with patch(
            "joiny_mnemonic.staleness.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2),
        ):
            self.assertEqual(
                self.service.stale(memory_id=record.id)[0].status,
                "unknown",
            )

        bad = self._memory(content="Bad timestamp", timestamp="not-a-timestamp")
        self.assertEqual(self.service.stale(memory_id=bad.id)[0].status, "unknown")

        with tempfile.TemporaryDirectory(prefix="joiny-nonrepo-") as directory:
            nonrepo = Path(directory)
            (nonrepo / "file.py").write_text("x = 1\n", encoding="utf-8")
            other = MemoryService(nonrepo / "memory.db", project_root=nonrepo)
            try:
                with patch("joiny_mnemonic.storage._now", return_value=BASELINE):
                    source = other.store.append_event(kind="message", content="source")
                    memory = other.derive_memory(
                        memory_type="fact",
                        content="nonrepo",
                        source_event_ids=(source.id,),
                        files=("file.py",),
                    )
                self.assertEqual(other.stale(memory_id=memory.id)[0].status, "unknown")
            finally:
                other.close()

    def test_cli_and_existing_mcp_search_expose_staleness_without_new_tool(self) -> None:
        record = self._memory(content="CLI stale probe")
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "joiny_mnemonic",
                "--db",
                str(self.database),
                "--project-root",
                str(self.root),
                "stale",
                "--id",
                record.id,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        output = json.loads(completed.stdout)
        self.assertEqual(output[0]["memory_id"], record.id)
        self.assertEqual(output[0]["status"], "current")

        hits = MCPServer(self.service)._call_tool(
            "memory_search",
            {
                "query": "CLI stale probe",
                "include_events": False,
                "semantic": False,
                "include_staleness": True,
            },
        )
        hit = next(item for item in hits if item.id == record.id)
        self.assertEqual(hit.metadata["staleness"]["status"], "current")


if __name__ == "__main__":
    unittest.main()