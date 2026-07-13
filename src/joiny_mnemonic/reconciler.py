"""Evidence-bound state maintenance (task5.md Part A).

The trust model protects writes; this module maintains truth. A completed
task whose completing event is already captured in the canonical log must
not keep rotting in a protected block until a human notices (live-run
finding: "создать файл delme2.md" stayed open while its Write event sat
105 seconds later in the same database, and two hosts then paraphrased the
stale entry into its opposite).

Everything here is deterministic and provenance-bound. Detection appends
canonical `task_completion_detected` events (idempotent via receipts).
Closure — rewriting the block without the completed entry — happens only
under the policy-ledger flag `automatic_task_closure_enabled`; with the
flag off, detections surface as pending findings in capabilities and the
resume packet. Nothing is ever deleted: block history keeps every version
and the task memory is superseded, not removed.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .models import Event

if TYPE_CHECKING:
    from .service import MemoryService


_PATH = re.compile(r"[\w\-./\\]+\.[A-Za-z0-9]{1,8}\b")
_BACKTICK = re.compile(r"`([^`\n]+)`")

# v1 verb gate: only creation/modification tasks accept write-tool evidence.
# Deletion tasks are skipped (a Write event must never "complete" a delete);
# they need absence evidence, which is a later, separate rule.
_CREATE_VERBS = re.compile(
    r"\b(созда|добав|напис|write|creat|add|обнов|updat|исправ|fix)", re.IGNORECASE
)
_DELETE_VERBS = re.compile(r"\b(удал|delete|remove|снес|drop)", re.IGNORECASE)

_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "create_file"}

# Hygiene path tokens must look like real project files (review finding L3:
# "e.g", "v1.2.3" and "example.com" are not paths).
_PATH_EXTENSIONS = {
    "py", "md", "js", "ts", "tsx", "jsx", "json", "yaml", "yml", "toml",
    "txt", "rs", "go", "java", "cs", "cpp", "c", "h", "sql", "sh", "ps1",
    "html", "css", "ini", "cfg", "csv",
}


def _hygiene_paths(entry: str) -> list[str]:
    return [
        token
        for token in _PATH.findall(entry)
        if token.rsplit(".", 1)[-1].casefold() in _PATH_EXTENSIONS
    ]


def _default_age_days() -> float:
    raw = os.environ.get("JOINY_MNEMONIC_TASK_AGE_DAYS", "")
    try:
        return float(raw) if raw else 14.0
    except ValueError:
        return 14.0


def _command_matches(entry_command: str, command_text: str) -> bool:
    """Exact or command-prefix match (review finding L1): `pytest -q` is
    evidence for "pytest -q --tb=short" but not for "echo never run
    pytest -q"."""
    text = " ".join(command_text.split())
    command = " ".join(entry_command.split())
    return text == command or text.startswith(command + " ")


@dataclass(frozen=True, slots=True)
class CompletionDetection:
    entry: str
    anchor_event_id: str | None
    evidence_event_id: str
    evidence_kind: str  # "file" | "command"
    evidence_detail: str


def _normalized(entry: str) -> str:
    return " ".join(entry.casefold().split())


def _entry_lines(content: str) -> list[str]:
    lines = []
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            lines.append(line)
    return lines


def _path_matches(entry_path: str, event_file: str) -> bool:
    """Suffix match on a path-segment boundary (review finding H2:
    'config.py' must not match 'test_config.py')."""
    entry_norm = entry_path.replace("\\", "/").casefold().lstrip("./")
    event_norm = str(event_file).replace("\\", "/").casefold()
    if not entry_norm or not event_norm.endswith(entry_norm):
        return False
    boundary = len(event_norm) - len(entry_norm) - 1
    return boundary < 0 or event_norm[boundary] == "/"


# Trust filter for completion evidence (review finding H1) now lives in SQL
# (completion_evidence_events: host_hook channel + tool_output kind); the
# PostToolUseFailure exclusion remains a Python-side check in the scan loop.


def _event_tool_name(event: Event) -> str:
    return str(event.payload.get("tool_name", "")).casefold()


def _event_command(event: Event) -> str:
    tool_input = event.payload.get("tool_input")
    if isinstance(tool_input, dict):
        return str(tool_input.get("command", ""))
    return ""


class StateReconciler:
    """Deterministic reconciliation of protected task state against canon."""

    def __init__(self, service: "MemoryService") -> None:
        self.service = service
        self.store = service.store

    # --- detection ---------------------------------------------------------

    def _task_anchor_map(
        self, branch_id: str
    ) -> dict[str, tuple[int, str | None, str]]:
        """One pass over task memories (review finding M5): normalized
        content -> (anchor seq, anchor event id, memory id)."""
        anchors: dict[str, tuple[int, str | None, str]] = {}
        for record in self.store.list_memories(
            branch_id=branch_id, memory_types=("task",)
        ):
            key = _normalized(record.content)
            if key in anchors:
                continue
            anchor_seq, anchor_id = 0, None
            if record.source_event_ids:
                try:
                    source = self.store.get_event(record.source_event_ids[0])
                    anchor_seq, anchor_id = source.seq, source.id
                except KeyError:
                    pass
            anchors[key] = (anchor_seq, anchor_id, record.id)
        return anchors

    def detect_completions(
        self, *, branch_id: str = "main"
    ) -> list[tuple[CompletionDetection, str | None]]:
        block = self.store.get_active_blocks(branch_id=branch_id).get("open_tasks")
        if block is None or not block.content.strip():
            return []
        anchors = self._task_anchor_map(branch_id)
        candidates: list[tuple[str, int, str, str | None]] = []
        for entry in _entry_lines(block.content):
            if _DELETE_VERBS.search(entry):
                continue
            anchor_seq, anchor_id, task_memory_id = anchors.get(
                _normalized(entry), (0, None, None)
            )
            if anchor_id is None:
                # Fail closed (review finding H3): without a provable
                # admission point, pre-task history would count as
                # completion evidence.
                continue
            candidates.append((entry, anchor_seq, anchor_id, task_memory_id))
        if not candidates:
            return []
        events = self.store.completion_evidence_events(
            branch_id=branch_id,
            after_seq=min(anchor_seq for _, anchor_seq, _, _ in candidates),
        )
        detections: list[tuple[CompletionDetection, str | None]] = []
        for entry, anchor_seq, anchor_id, task_memory_id in candidates:
            paths = _PATH.findall(entry) if _CREATE_VERBS.search(entry) else []
            commands = [item.strip() for item in _BACKTICK.findall(entry) if item.strip()]
            evidence: tuple[str, str, str] | None = None
            for event in events:
                if event.seq <= anchor_seq:
                    continue
                if event.payload.get("hook_event_name") == "PostToolUseFailure":
                    continue  # SQL prefilter covers kind/channel, not failures
                if paths and _event_tool_name(event) in _WRITE_TOOLS:
                    for path in paths:
                        if any(_path_matches(path, item) for item in event.files):
                            evidence = (event.id, "file", path)
                            break
                if evidence is None and commands:
                    command_text = _event_command(event)
                    if command_text:
                        for command in commands:
                            if _command_matches(command, command_text):
                                evidence = (event.id, "command", command)
                                break
                if evidence is not None:
                    break
            if evidence is not None:
                detections.append(
                    (
                        CompletionDetection(
                            entry=entry,
                            anchor_event_id=anchor_id,
                            evidence_event_id=evidence[0],
                            evidence_kind=evidence[1],
                            evidence_detail=evidence[2],
                        ),
                        task_memory_id,
                    )
                )
        return detections

    # --- reconciliation ----------------------------------------------------

    def _closure_enabled(self) -> bool:
        active = self.store.active_policy()
        return bool(
            active and active["policy"].get("automatic_task_closure_enabled", False)
        )

    def reconcile(self, *, branch_id: str = "main") -> dict[str, Any]:
        summary = {"detected": 0, "closed": 0, "pending": 0}
        detections = self.detect_completions(branch_id=branch_id)
        if not detections:
            return summary
        close = self._closure_enabled()
        for detection, task_memory_id in detections:
            summary["detected"] += 1
            receipt = "task-completion:{}:{}:{}".format(
                branch_id,
                hashlib.sha256(_normalized(detection.entry).encode()).hexdigest()[:16],
                detection.evidence_event_id,
            )
            events, created = self.store.append_internal_events_once(
                receipt,
                [
                    {
                        "kind": "state",
                        "role": None,
                        "content": f"task completion detected: {detection.entry}",
                        "payload": {
                            "operation": "task_completion_detected",
                            **asdict(detection),
                            "task_memory_id": task_memory_id,
                        },
                    }
                ],
                branch_id=branch_id,
            )
            detection_event = events[0]
            if not close:
                summary["pending"] += 1
                continue
            block = self.store.get_active_blocks(branch_id=branch_id).get("open_tasks")
            if block is None:
                continue
            # Remove only the completed entry's line; untouched lines keep
            # their original bytes and formatting (review finding L2).
            target = _normalized(detection.entry)
            raw_lines = block.content.splitlines()
            remaining = [
                raw
                for raw in raw_lines
                if _normalized(raw.strip().removeprefix("- ").strip()) != target
            ]
            if len(remaining) == len(raw_lines):
                continue  # already closed by an earlier run
            self.store.set_active_block(
                "open_tasks",
                "\n".join(remaining),
                branch_id=branch_id,
                source_event_ids=(detection_event.id, detection.evidence_event_id),
            )
            if task_memory_id is not None:
                record = self.store.get_memory(task_memory_id)
                if record.metadata.get("status") == "completed":
                    summary["closed"] += 1
                    continue
                # Gate on the record's own state, not on detection-event
                # freshness (review finding M4): a detection made while the
                # flag was off must still supersede on a later closure run.
                self.store.derive_memory(
                    memory_type="task",
                    content=record.content,
                    summary=record.summary,
                    source_event_ids=(detection_event.id,),
                    branch_id=branch_id,
                    supersedes_id=record.id,
                    metadata={
                        **record.metadata,
                        "status": "completed",
                        "completed_by": detection.evidence_event_id,
                    },
                )
            summary["closed"] += 1
        return summary

    def pending_completions(self, *, branch_id: str = "main") -> list[dict[str, Any]]:
        """Detections whose entry is still in the live block (flag off, or
        detected before the flag was enabled)."""
        block = self.store.get_active_blocks(branch_id=branch_id).get("open_tasks")
        live = {_normalized(line) for line in _entry_lines(block.content)} if block else set()
        if not live:
            return []
        pending: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in self.store.events_by_operation(
            "task_completion_detected", branch_id=branch_id
        ):
            payload = event.payload
            entry = _normalized(str(payload.get("entry", "")))
            if entry in live and entry not in seen:
                seen.add(entry)
                pending.append(
                    {
                        "entry": payload.get("entry"),
                        "evidence_event_id": payload.get("evidence_event_id"),
                        "detection_event_id": event.id,
                    }
                )
        return pending

    # --- hygiene -----------------------------------------------------------

    def hygiene_findings(self, *, branch_id: str = "main") -> list[dict[str, str]]:
        """Warning-only, recomputed on demand, ranking-neutral — the same
        contract as staleness."""
        findings: list[dict[str, str]] = []
        blocks = self.store.get_active_blocks(branch_id=branch_id)
        now = datetime.now(UTC)
        age_days = _default_age_days()
        tasks = blocks.get("open_tasks")
        if tasks:
            root = self.service.project_root.resolve()
            anchors = self._task_anchor_map(branch_id)
            for entry in _entry_lines(tasks.content):
                for path in _hygiene_paths(entry):
                    candidate = (root / path.replace("\\", "/")).resolve()
                    if root not in candidate.parents and candidate != root:
                        continue  # never probe outside the project (L3)
                    if _DELETE_VERBS.search(entry) is None and _CREATE_VERBS.search(entry):
                        continue  # creation targets legitimately absent
                    if not candidate.exists():
                        findings.append(
                            {
                                "finding": "task_references_missing_file",
                                "entry": entry,
                                "path": path,
                            }
                        )
                anchor_seq, anchor_id, _ = anchors.get(
                    _normalized(entry), (0, None, None)
                )
                if anchor_id is not None:
                    try:
                        created = datetime.fromisoformat(
                            self.store.get_event(anchor_id).created_at
                        )
                    except (KeyError, ValueError):
                        created = None
                    if created is not None and (
                        (now - created).total_seconds() > age_days * 86400
                    ):
                        findings.append(
                            {
                                "finding": "task_open_beyond_age_threshold",
                                "entry": entry,
                                "anchor_event_id": anchor_id,
                            }
                        )
        decisions = blocks.get("decisions")
        if decisions:
            for entry in _entry_lines(decisions.content):
                if entry.rstrip().endswith("?"):
                    findings.append(
                        {"finding": "decision_entry_is_a_question", "entry": entry}
                    )
        return findings
