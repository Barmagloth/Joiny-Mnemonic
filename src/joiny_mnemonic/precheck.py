from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import MemoryRecord
from .staleness import StalenessService
from .storage import MemoryStore


@dataclass(frozen=True, slots=True)
class PrecheckFinding:
    code: str
    severity: str
    title: str
    details: tuple[str, ...]
    files: tuple[str, ...]
    memory_ids: tuple[str, ...]
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrecheckReport:
    findings: tuple[PrecheckFinding, ...]
    files: tuple[str, ...]
    command: str | None
    blocked: bool


_SEVERITY_ORDER = {"block": 0, "warn": 1, "info": 2}
_TOKEN = re.compile(r'''"[^"]*"|'[^']*'|\S+''')
_COMMAND_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "command_force_push",
        re.compile(r"(?i)\bgit\s+push\b[^\r\n]*(?:--force-with-lease|--force)\b"),
        "Force-push can overwrite remote history.",
    ),
    (
        "command_hard_reset",
        re.compile(r"(?i)\bgit\s+reset\b[^\r\n]*--hard\b"),
        "Hard reset can discard local work.",
    ),
    (
        "command_terraform_destroy",
        re.compile(r"(?i)\bterraform\s+destroy\b"),
        "Terraform destroy removes managed infrastructure.",
    ),
    (
        "command_kubectl_delete_namespace",
        re.compile(r"(?i)\bkubectl\s+delete\s+(?:namespace|ns)\b"),
        "Deleting a namespace removes all namespaced resources.",
    ),
    (
        "command_drop_database",
        re.compile(r"(?i)\bDROP\s+(?:DATABASE|SCHEMA)\b"),
        "Dropping a database or schema destroys persistent data.",
    ),
)


class PrecheckService:
    def __init__(
        self,
        store: MemoryStore,
        staleness: StalenessService,
        project_root: str | Path,
        *,
        git_timeout_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.staleness = staleness
        self.project_root = Path(project_root).resolve()
        self.git_timeout_seconds = git_timeout_seconds

    @staticmethod
    def _record_details(records: Sequence[MemoryRecord]) -> tuple[str, ...]:
        return tuple(
            f"{record.id}: {record.summary or record.content}"
            for record in sorted(records, key=lambda item: item.id)
        )

    @staticmethod
    def _record_ids(
        records: Sequence[MemoryRecord],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        ordered = sorted(records, key=lambda item: item.id)
        memory_ids = tuple(record.id for record in ordered)
        source_ids = tuple(
            dict.fromkeys(
                event_id
                for record in ordered
                for event_id in record.source_event_ids
            )
        )
        return memory_ids, source_ids

    @staticmethod
    def _finding_for_records(
        *,
        code: str,
        severity: str,
        title: str,
        file: str,
        records: Sequence[MemoryRecord],
    ) -> PrecheckFinding:
        memory_ids, source_ids = PrecheckService._record_ids(records)
        return PrecheckFinding(
            code=code,
            severity=severity,
            title=title,
            details=PrecheckService._record_details(records),
            files=(file,),
            memory_ids=memory_ids,
            source_event_ids=source_ids,
        )

    def _file_findings(
        self, files: Sequence[str], *, branch_id: str
    ) -> list[PrecheckFinding]:
        findings: list[PrecheckFinding] = []
        stale_candidates: dict[str, MemoryRecord] = {}
        for file in files:
            records = self.store.list_memories(branch_id=branch_id, file=file)
            for memory_type, code, title in (
                ("failure", "known_failure", f"Prior failures reference {file}."),
                ("lesson", "known_lesson", f"Prior lessons reference {file}."),
            ):
                selected = [
                    record for record in records
                    if record.memory_type == memory_type
                    and self.store.memory_authority(record.id) == "confirmed"
                ]
                if selected:
                    findings.append(
                        self._finding_for_records(
                            code=code,
                            severity="warn",
                            title=title,
                            file=file,
                            records=selected,
                        )
                    )

            tasks = [record for record in records if record.memory_type == "task"]
            if tasks:
                findings.append(
                    self._finding_for_records(
                        code="active_task_memory",
                        severity="info",
                        title=f"Task memories reference {file}.",
                        file=file,
                        records=tasks,
                    )
                )
            for record in records:
                if record.memory_type in {"decision", "fact", "lesson"}:
                    stale_candidates[record.id] = record

        if stale_candidates:
            inspections = self.staleness.inspect(
                branch_id=branch_id,
                memory_ids=tuple(stale_candidates),
            )
            for inspection in inspections:
                if inspection.status != "possibly_stale":
                    continue
                record = stale_candidates[inspection.memory_id]
                findings.append(
                    PrecheckFinding(
                        code="possibly_stale_memory",
                        severity="warn",
                        title=f"Memory {record.id} may be stale.",
                        details=inspection.reasons,
                        files=record.files,
                        memory_ids=(record.id,),
                        source_event_ids=record.source_event_ids,
                    )
                )

        file_set = set(files)
        for task in self.store.list_tasks():
            if task.status not in {"active", "blocked"} or task.branch_id != branch_id:
                continue
            relevant = tuple(
                sorted(file_set & set(self._metadata_files(task.metadata)))
            )
            if relevant:
                findings.append(
                    PrecheckFinding(
                        code="active_task_context",
                        severity="info",
                        title=f"Active task {task.task_key}: {task.title}",
                        details=(f"status={task.status}",),
                        files=relevant,
                        memory_ids=(),
                        source_event_ids=task.source_event_ids,
                    )
                )

        constraints = self.store.get_active_blocks(branch_id=branch_id).get("constraints")
        if constraints is not None:
            findings.append(
                PrecheckFinding(
                    code="active_constraints",
                    severity="info",
                    title="Active constraints apply to this action.",
                    details=tuple(
                        line.strip()
                        for line in constraints.content.splitlines()
                        if line.strip()
                    ),
                    files=tuple(files),
                    memory_ids=(),
                    source_event_ids=constraints.source_event_ids,
                )
            )
        return findings

    @staticmethod
    def _metadata_files(metadata: Mapping[str, Any]) -> tuple[str, ...]:
        found: list[str] = []
        for key in ("file", "file_path", "path", "files", "paths"):
            value = metadata.get(key)
            values = value if isinstance(value, (list, tuple, set)) else (value,)
            for item in values:
                if item is None:
                    continue
                text = str(item).strip()
                if text and text not in found:
                    found.append(text)
        return tuple(found)

    def _protected_delete_target(self, command: str) -> bool:
        tokens = [item.strip(chr(34) + chr(39)) for item in _TOKEN.findall(command)]
        lowered = [item.casefold() for item in tokens]
        recursive = False
        targets: list[str] = []
        if "rm" in lowered:
            index = lowered.index("rm")
            arguments = tokens[index + 1 :]
            recursive = any(
                "r" in item.casefold()
                for item in arguments
                if item.startswith("-")
            )
            targets = [item for item in arguments if not item.startswith("-")]
        elif "remove-item" in lowered:
            index = lowered.index("remove-item")
            arguments = tokens[index + 1 :]
            recursive = any(
                item.casefold() in {"-recurse", "-r"} for item in arguments
            )
            targets = [item for item in arguments if not item.startswith("-")]
        if not recursive:
            return False

        root = self.project_root.as_posix().casefold().rstrip("/")
        home = Path.home().resolve().as_posix().casefold().rstrip("/")
        for target in targets:
            normalized = target.replace("\\", "/").casefold()
            direct = normalized.rstrip("*")
            stripped = direct.rstrip("/")
            if direct in {"/", "~", "$home", "%userprofile%", "."}:
                return True
            if re.fullmatch(r"[a-z]:", stripped):
                return True
            if stripped in {root, home}:
                return True
        return False

    def _command_findings(self, command: str | None) -> list[PrecheckFinding]:
        if not command:
            return []
        safe_command, redactions = self.store.redactor.redact_text(command)
        findings: list[PrecheckFinding] = []
        if self._protected_delete_target(command):
            findings.append(
                PrecheckFinding(
                    code="command_recursive_delete",
                    severity="warn",
                    title="Recursive deletion targets a protected root.",
                    details=(safe_command,),
                    files=(),
                    memory_ids=(),
                    source_event_ids=(),
                )
            )
        for code, pattern, title in _COMMAND_RULES:
            if pattern.search(command):
                findings.append(
                    PrecheckFinding(
                        code=code,
                        severity="warn",
                        title=title,
                        details=(safe_command,),
                        files=(),
                        memory_ids=(),
                        source_event_ids=(),
                    )
                )
        if any(item.rule != "private_region" for item in redactions):
            findings.append(
                PrecheckFinding(
                    code="command_inline_secret",
                    severity="warn",
                    title="Command appears to contain an inline credential.",
                    details=(safe_command,),
                    files=(),
                    memory_ids=(),
                    source_event_ids=(),
                )
            )
        return findings

    def _staged_files(self) -> tuple[tuple[str, ...], str | None]:
        try:
            completed = subprocess.run(
                [
                    "git", "-C", str(self.project_root),
                    "diff", "--cached", "--name-only", "-z",
                ],
                capture_output=True,
                text=True,
                timeout=self.git_timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return (), "git executable is unavailable"
        except subprocess.TimeoutExpired:
            return (), "git staged-file query timed out"
        except OSError as exc:
            return (), f"git staged-file query failed: {exc}"
        if completed.returncode != 0:
            detail = next(
                (
                    line.strip()
                    for line in completed.stderr.splitlines()
                    if line.strip()
                ),
                f"exit {completed.returncode}",
            )
            return (), f"git staged-file query failed: {detail}"
        return tuple(
            sorted(
                dict.fromkeys(
                    item for item in completed.stdout.split(chr(0)) if item
                )
            )
        ), None

    @staticmethod
    def _deduplicate(findings: Iterable[PrecheckFinding]) -> tuple[PrecheckFinding, ...]:
        unique = {
            (
                finding.code,
                finding.severity,
                finding.title,
                finding.details,
                finding.files,
                finding.memory_ids,
                finding.source_event_ids,
            ): finding
            for finding in findings
        }
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    _SEVERITY_ORDER.get(item.severity, 99),
                    item.files[0] if item.files else "",
                    item.code,
                    item.source_event_ids[0] if item.source_event_ids else "",
                ),
            )
        )

    def run(
        self,
        *,
        files: Sequence[str] = (),
        staged: bool = False,
        command: str | None = None,
        branch_id: str = "main",
    ) -> PrecheckReport:
        selected_files = list(dict.fromkeys(str(item) for item in files if str(item)))
        findings: list[PrecheckFinding] = []
        if staged:
            staged_files, error = self._staged_files()
            selected_files.extend(
                item for item in staged_files if item not in selected_files
            )
            if error is not None:
                findings.append(
                    PrecheckFinding(
                        code="staged_files_unavailable",
                        severity="warn",
                        title="Staged files could not be inspected.",
                        details=(error,),
                        files=(),
                        memory_ids=(),
                        source_event_ids=(),
                    )
                )
        ordered_files = tuple(sorted(selected_files))
        findings.extend(self._file_findings(ordered_files, branch_id=branch_id))
        findings.extend(self._command_findings(command))
        ordered_findings = self._deduplicate(findings)
        return PrecheckReport(
            findings=ordered_findings,
            files=ordered_files,
            command=(
                self.store.redactor.redact_text(command)[0]
                if command is not None
                else None
            ),
            blocked=any(item.severity == "block" for item in ordered_findings),
        )

    @staticmethod
    def from_dict(value: Mapping[str, Any]) -> PrecheckReport:
        return PrecheckReport(
            findings=tuple(
                PrecheckFinding(
                    code=str(item["code"]),
                    severity=str(item["severity"]),
                    title=str(item["title"]),
                    details=tuple(str(value) for value in item.get("details", ())),
                    files=tuple(str(value) for value in item.get("files", ())),
                    memory_ids=tuple(str(value) for value in item.get("memory_ids", ())),
                    source_event_ids=tuple(
                        str(value) for value in item.get("source_event_ids", ())
                    ),
                )
                for item in value.get("findings", ())
            ),
            files=tuple(str(item) for item in value.get("files", ())),
            command=(
                str(value["command"]) if value.get("command") is not None else None
            ),
            blocked=bool(value.get("blocked", False)),
        )

    @staticmethod
    def render(report: PrecheckReport, *, max_bytes: int = 4096) -> str:
        if not report.findings or max_bytes < 64:
            return ""

        def ids(label: str, values: tuple[str, ...]) -> str:
            shown = values[:4]
            suffix = f",+{len(values) - len(shown)} more" if len(values) > len(shown) else ""
            return f"{label}=" + ",".join(shown) + suffix if shown else ""

        candidates = ["[JOINY PRECHECK]"]
        for finding in report.findings:
            identifiers = " ".join(
                part
                for part in (
                    ids("memory_ids", finding.memory_ids),
                    ids("source_event_ids", finding.source_event_ids),
                )
                if part
            )
            candidates.append(
                f"{finding.severity.upper()} {finding.title}"
                + (f" [{identifiers}]" if identifiers else "")
            )
            candidates.extend(f"- {detail}" for detail in finding.details[:3])
        candidates.append("Inspect exact evidence before repeating a failed approach.")

        suffix = "[JOINY PRECHECK TRUNCATED]"
        lines: list[str] = []
        for line in candidates:
            proposed = "\n".join([*lines, line])
            if len(proposed.encode("utf-8")) <= max_bytes:
                lines.append(line)
                continue
            truncated = "\n".join([*lines, suffix])
            while lines and len(truncated.encode("utf-8")) > max_bytes:
                lines.pop()
                truncated = "\n".join([*lines, suffix])
            return truncated if len(truncated.encode("utf-8")) <= max_bytes else ""
        return "\n".join(lines)

    @staticmethod
    def as_dict(report: PrecheckReport) -> dict[str, Any]:
        return asdict(report)