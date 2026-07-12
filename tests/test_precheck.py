from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from joiny_mnemonic.adapters import adapter_capabilities
from joiny_mnemonic.hooks import install_git_precommit, install_hooks, process_hook
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
BASELINE = "2020-01-01T00:00:00+00:00"


class PrecheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_ROOT / f"precheck-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self._git("init")
        self._git("config", "user.email", "joiny@example.test")
        self._git("config", "user.name", "Joiny Test")
        self._git("config", "core.hooksPath", ".git/hooks")
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

    def _memory(
        self,
        memory_type: str,
        content: str,
        *,
        branch_id: str = "main",
        file: str = "src/auth.py",
    ):
        with patch("joiny_mnemonic.storage._now", return_value=BASELINE):
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
            )

    def _advance_file(self, count: int) -> None:
        for index in range(1, count + 1):
            self.file.write_text(f"version = {index}\n", encoding="utf-8")
            self._git("add", "src/auth.py")
            self._git("commit", "-m", f"change {index}")

    def test_file_findings_include_exact_ids_staleness_tasks_and_constraints(self) -> None:
        failure = self._memory("failure", "Deadlock during refresh rotation.")
        lesson = self._memory("lesson", "Verify transaction ownership before retry.")
        fact = self._memory("fact", "Auth middleware owns the transaction.")
        task_memory = self._memory("task", "Update auth retry handling.")
        constraint_source = self.service.store.append_event(
            kind="message", role="user", content="Constraint source"
        )
        constraint = self.service.store.set_active_block(
            "constraints",
            "Never bypass auth checks.",
            source_event_ids=(constraint_source.id,),
        )
        task_source = self.service.store.append_event(
            kind="state", content="Task source"
        )
        task = self.service.store.create_task_version(
            task_key="AUTH-1",
            branch_id="main",
            title="Fix auth retries",
            status="active",
            source_event_ids=(task_source.id,),
            metadata={"files": ["src/auth.py"]},
        )
        self._advance_file(2)

        report = self.service.precheck(files=("src/auth.py", "src/auth.py"))
        codes = [finding.code for finding in report.findings]

        self.assertEqual(report.files, ("src/auth.py",))
        self.assertIn("known_failure", codes)
        self.assertIn("known_lesson", codes)
        self.assertIn("possibly_stale_memory", codes)
        self.assertIn("active_task_memory", codes)
        self.assertIn("active_task_context", codes)
        self.assertIn("active_constraints", codes)
        failure_finding = next(
            finding for finding in report.findings if finding.code == "known_failure"
        )
        self.assertEqual(failure_finding.memory_ids, (failure.id,))
        self.assertEqual(failure_finding.source_event_ids, failure.source_event_ids)
        self.assertTrue(
            {lesson.id, fact.id}.issubset(
                {
                    finding.memory_ids[0]
                    for finding in report.findings
                    if finding.code == "possibly_stale_memory"
                }
            )
        )
        task_finding = next(
            finding for finding in report.findings if finding.code == "active_task_context"
        )
        self.assertEqual(task_finding.source_event_ids, task.source_event_ids)
        constraint_finding = next(
            finding for finding in report.findings if finding.code == "active_constraints"
        )
        self.assertEqual(constraint_finding.source_event_ids, constraint.source_event_ids)
        self.assertIn(task_memory.id, next(
            finding for finding in report.findings
            if finding.code == "active_task_memory"
        ).memory_ids)
        severities = [finding.severity for finding in report.findings]
        self.assertEqual(
            severities,
            sorted(severities, key={"block": 0, "warn": 1, "info": 2}.get),
        )
        self.assertFalse(report.blocked)

    def test_dangerous_command_rules_warn_and_redact_inline_secret(self) -> None:
        cases = {
            "rm -rf /": "command_recursive_delete",
            f'Remove-Item "{self.root}" -Recurse -Force': "command_recursive_delete",
            "git push --force-with-lease origin main": "command_force_push",
            "git reset --hard HEAD~1": "command_hard_reset",
            "terraform destroy -auto-approve": "command_terraform_destroy",
            "kubectl delete namespace production": "command_kubectl_delete_namespace",
            "DROP DATABASE production": "command_drop_database",
        }
        for command, code in cases.items():
            with self.subTest(command=command):
                report = self.service.precheck(command=command)
                self.assertIn(code, {item.code for item in report.findings})
                self.assertFalse(report.blocked)

        secret = "Bearer abcdefghijklmnop"
        report = self.service.precheck(
            command=f'curl -H "Authorization: {secret}" https://example.test'
        )
        finding = next(
            item for item in report.findings if item.code == "command_inline_secret"
        )
        self.assertNotIn(secret, finding.details[0])
        self.assertIn("[REDACTED:bearer_token]", finding.details[0])

    def test_staged_files_cli_and_git_unavailable_are_operational(self) -> None:
        self._memory("failure", "Staged auth failure.")
        self.file.write_text("version = 99\n", encoding="utf-8")
        self._git("add", "src/auth.py")

        report = self.service.precheck(staged=True)
        self.assertEqual(report.files, ("src/auth.py",))
        self.assertIn("known_failure", {item.code for item in report.findings})

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
                "precheck",
                "--staged",
                "--command",
                "git push --force origin main",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertFalse(output["blocked"])
        self.assertIn(
            "command_force_push",
            {item["code"] for item in output["findings"]},
        )

        with patch(
            "joiny_mnemonic.precheck.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            unavailable = self.service.precheck(staged=True)
        self.assertIn(
            "staged_files_unavailable",
            {item.code for item in unavailable.findings},
        )

    def test_branch_isolation_excludes_parent_memories_created_after_fork(self) -> None:
        visible = self._memory("failure", "Visible before fork.")
        self.service.store.create_branch("child")
        hidden = self._memory("failure", "Hidden after fork.")

        child = self.service.precheck(
            files=("src/auth.py",),
            branch_id="child",
        )
        ids = {
            memory_id
            for finding in child.findings
            for memory_id in finding.memory_ids
        }
        self.assertIn(visible.id, ids)
        self.assertNotIn(hidden.id, ids)

    def test_pretooluse_injects_bounded_protocol_valid_idempotent_warning(self) -> None:
        failure = self._memory("failure", "Prior auth write failed.")
        value = {
            "hook_event_name": "PreToolUse",
            "session_id": "pretool-1",
            "tool_use_id": "pretool-call-1",
            "tool_name": "Shell",
            "tool_input": {
                "command": "git push --force origin main",
                "file_path": "src/auth.py",
            },
        }

        first = process_hook(self.service, "claude-code", value)
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            first["hookSpecificOutput"]["hookEventName"],
            "PreToolUse",
        )
        self.assertIn("[JOINY PRECHECK]", context)
        self.assertIn(failure.id, context)
        self.assertIn(failure.source_event_ids[0], context)
        self.assertLessEqual(len(context.encode("utf-8")), 4096)
        self.assertNotIn("permissionDecision", first)
        self.assertTrue(adapter_capabilities("claude-code")["pre_action_precheck"])
        self.assertFalse(adapter_capabilities("codex")["pre_action_precheck"])
        install_hooks("claude-code", self.root)
        claude = json.loads(
            (self.root / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertIn("PreToolUse", claude["hooks"])

        self._memory("failure", "A later failure must not alter retry output.")
        second = process_hook(self.service, "claude-code", value)
        self.assertEqual(second, first)
        deliveries = [
            event
            for event in self.service.store.query_events(kinds=("state",))
            if event.payload.get("hook_event_name") == "PreToolUse"
        ]
        self.assertEqual(len(deliveries), 1)
        self.assertIn("_joiny_precheck", deliveries[0].payload)

    def test_warning_budget_and_git_hook_installer(self) -> None:
        for index in range(80):
            self._memory("failure", f"Failure {index:03d} " + "x" * 80)
        report = self.service.precheck(files=("src/auth.py",))
        rendered = self.service.prechecks.render(report, max_bytes=1024)
        self.assertLessEqual(len(rendered.encode("utf-8")), 1024)
        self.assertIn("[JOINY PRECHECK", rendered)

        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")
        first = install_git_precommit(self.root)
        second = install_git_precommit(self.root)
        content = hook.read_text(encoding="utf-8")
        self.assertIn("echo existing", content)
        self.assertIn("precheck --staged", content)
        self.assertEqual(content.count("# joiny-mnemonic precheck begin"), 1)
        self.assertEqual(first["status"], "installed")
        self.assertEqual(second["status"], "updated")

        self.file.write_text("version = 101\n", encoding="utf-8")
        self._git("add", "src/auth.py")
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        committed = subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "exercise precheck hook"],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.assertTrue((self.root / ".joiny-mnemonic" / "memory.db").exists())


if __name__ == "__main__":
    unittest.main()