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
from pathlib import Path
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

    def _closure_flag(self) -> bool:
        active = self.store.active_policy()
        return bool(
            active and active["policy"].get("automatic_task_closure_enabled", False)
        )

    @staticmethod
    def _strength(evidence_kind: str) -> str:
        # Deterministic evidence-strength ladder (task6.md 6B): a trusted
        # host-hook write of the exact path is strong; a command prefix
        # match is medium; anything else stays pending.
        return {"file": "strong", "command": "medium"}.get(evidence_kind, "weak")

    def _auto_apply_allowed(self, strength: str) -> bool:
        # Automation first: strong evidence auto-applies BY DEFAULT (cheap
        # lossless undo is what licenses this); medium follows the legacy
        # policy flag; weak never auto-applies.
        if strength == "strong":
            return True
        if strength == "medium":
            return self._closure_flag()
        return False

    def reconcile(self, *, branch_id: str = "main") -> dict[str, Any]:
        summary: dict[str, Any] = {
            "detected": 0, "closed": 0, "pending": 0, "auto_closed": [],
            "invalidated": self.invalidated_closures(branch_id=branch_id),
        }
        detections = self.detect_completions(branch_id=branch_id)
        if not detections:
            return summary
        for detection, task_memory_id in detections:
            summary["detected"] += 1
            receipt = "task-completion:{}:{}:{}".format(
                branch_id,
                hashlib.sha256(_normalized(detection.entry).encode()).hexdigest()[:16],
                detection.evidence_event_id,
            )
            events, _created = self.store.append_internal_events_once(
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
            strength = self._strength(detection.evidence_kind)
            candidate_id, _fresh, status = self.store.create_settlement_candidate(
                kind="task_closure",
                content=detection.entry,
                source_event_id=detection_event.id,
                evidence_event_id=detection.evidence_event_id,
                strength=strength,
            )
            if status != "pending":
                # Consume-once: an applied candidate was handled; a reverted
                # or contested one must never re-apply from the same evidence.
                continue
            if not self._auto_apply_allowed(strength):
                summary["pending"] += 1
                continue
            if self._apply_closure(
                detection, task_memory_id, candidate_id,
                detection_event=detection_event, branch_id=branch_id,
                rule_id=f"auto_closure_{strength}_evidence",
            ):
                summary["closed"] += 1
                summary["auto_closed"].append(
                    {
                        "entry": detection.entry,
                        "candidate_id": candidate_id,
                        "evidence_event_id": detection.evidence_event_id,
                    }
                )
            else:
                summary["pending"] += 1
        return summary

    def _apply_closure(
        self,
        detection: CompletionDetection,
        task_memory_id: str | None,
        candidate_id: str,
        *,
        detection_event: Event,
        branch_id: str,
        rule_id: str,
        actor: str = "system",
        settle_source_event_id: str | None = None,
    ) -> bool:
        block = self.store.get_active_blocks(branch_id=branch_id).get("open_tasks")
        if block is None:
            return False
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
            return False  # already closed by an earlier run
        applied_events, _ = self.store.append_internal_events_once(
            f"task-closure-applied:{candidate_id}",
            [
                {
                    "kind": "state",
                    "role": None,
                    "content": f"task closure applied: {detection.entry}",
                    "payload": {
                        "operation": "task_closure_applied",
                        "candidate_id": candidate_id,
                        "entry": detection.entry,
                        "evidence_event_id": detection.evidence_event_id,
                        # Audit evidence, not magic authority (task6.md):
                        # nothing here claims OS enforcement.
                        "enforcement_level": "recorded_only",
                    },
                }
            ],
            branch_id=branch_id,
        )
        self.store.set_active_block(
            "open_tasks",
            "\n".join(remaining),
            branch_id=branch_id,
            source_event_ids=(detection_event.id, detection.evidence_event_id),
        )
        if task_memory_id is not None:
            record = self.store.get_memory(task_memory_id)
            if record.metadata.get("status") != "completed":
                # Gate on the record's own state, not on detection-event
                # freshness (review finding M4).
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
                        "closure_candidate_id": candidate_id,
                    },
                )
        self.store.settle_candidate(
            candidate_id, "applied",
            source_event_id=settle_source_event_id or applied_events[0].id,
            actor=actor, rule_id=rule_id,
        )
        return True

    def apply_closure_candidate(
        self, candidate: dict[str, Any], *, branch_id: str = "main",
        actor: str, rule_id: str, settle_source_event_id: str,
    ) -> bool:
        """Manual apply of a pending task_closure candidate (task6.md 6C):
        reconstruct the detection from its canonical event and write through
        the same closure path as auto-apply."""
        detection_event = self.store.get_event(str(candidate["source_event_id"]))
        payload = detection_event.payload
        detection = CompletionDetection(
            entry=str(payload.get("entry") or candidate["normalized_content"]),
            anchor_event_id=payload.get("anchor_event_id"),
            evidence_event_id=str(
                payload.get("evidence_event_id") or candidate["evidence_quote"]
            ),
            evidence_kind=str(payload.get("evidence_kind", "")),
            evidence_detail=str(payload.get("evidence_detail", "")),
        )
        return self._apply_closure(
            detection,
            payload.get("task_memory_id"),
            str(candidate["id"]),
            detection_event=detection_event,
            branch_id=branch_id,
            rule_id=rule_id,
            actor=actor,
            settle_source_event_id=settle_source_event_id,
        )

    def undo_closure(
        self, candidate_id: str, *, branch_id: str = "main",
        rule_id: str = "operator_undo", detail: dict[str, Any] | None = None,
        actor: str = "system", settle_source_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Lossless revert of an applied closure: the entry line returns to
        open_tasks, the task memory gets a superseding 'reopened' version,
        the candidate records the round trip. Cheap undo is what licenses
        the automation."""
        candidates = {
            item["id"]: item for item in self.store.list_settlement_candidates(
                kind="task_closure"
            )
        }
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(f"unknown settlement candidate: {candidate_id}")
        entry = str(candidate["normalized_content"])
        revert_events, created = self.store.append_internal_events_once(
            f"task-closure-reverted:{candidate_id}",
            [
                {
                    "kind": "state",
                    "role": None,
                    "content": f"task closure reverted: {entry}",
                    "payload": {
                        "operation": "task_closure_reverted",
                        "candidate_id": candidate_id,
                        "entry": entry,
                        "rule_id": rule_id,
                        "enforcement_level": "recorded_only",
                        **(detail or {}),
                    },
                }
            ],
            branch_id=branch_id,
        )
        transition = self.store.settle_candidate(
            candidate_id, "reverted",
            source_event_id=settle_source_event_id or revert_events[0].id,
            actor=actor, rule_id=rule_id,
        )
        block = self.store.get_active_blocks(branch_id=branch_id).get("open_tasks")
        lines = block.content.splitlines() if block else []
        target = _normalized(entry)
        present = any(
            _normalized(line.strip().removeprefix("- ").strip()) == target
            for line in lines
        )
        if not present:
            content = "\n".join([*lines, f"- {entry}"]) if lines else f"- {entry}"
            self.store.set_active_block(
                "open_tasks", content, branch_id=branch_id,
                source_event_ids=(revert_events[0].id,),
            )
        for record in self.store.list_memories(
            branch_id=branch_id, memory_types=("task",)
        ):
            if (
                record.metadata.get("closure_candidate_id") == candidate_id
                and record.metadata.get("status") == "completed"
            ):
                self.store.derive_memory(
                    memory_type="task",
                    content=record.content,
                    summary=record.summary,
                    source_event_ids=(revert_events[0].id,),
                    branch_id=branch_id,
                    supersedes_id=record.id,
                    metadata={
                        **record.metadata, "status": "reopened",
                        "reopened_by": revert_events[0].id,
                    },
                )
                break
        return {
            "candidate_id": candidate_id, "entry": entry,
            "transition_id": transition, "already_reverted": not created,
        }

    def contest_reasserted_entry(
        self, entry: str, *, source_event_id: str, branch_id: str = "main"
    ) -> list[str]:
        """Bidirectional reconciliation: a user marker re-adding a closed
        entry IS the correction signal. Applied closures matching the entry
        flip to contested and never re-apply from the same evidence."""
        target = _normalized(entry)
        contested: list[str] = []
        for item in self.store.list_settlement_candidates(kind="task_closure"):
            if (item.get("status") or "pending") != "applied":
                continue
            if _normalized(str(item["normalized_content"])) != target:
                continue
            transition = self.store.settle_candidate(
                str(item["id"]), "contested",
                source_event_id=source_event_id,
                actor="system", rule_id="task_reasserted_by_marker",
            )
            if transition is not None:
                contested.append(str(item["id"]))
        return contested

    def pending_completions(self, *, branch_id: str = "main") -> list[dict[str, Any]]:
        """Settlement candidates awaiting confirmation whose entry is still
        in the live block (medium/weak evidence, or auto-apply declined)."""
        block = self.store.get_active_blocks(branch_id=branch_id).get("open_tasks")
        live = {_normalized(line) for line in _entry_lines(block.content)} if block else set()
        if not live:
            return []
        pending: list[dict[str, Any]] = []
        for item in self.store.list_settlement_candidates(
            kind="task_closure", status="pending"
        ):
            entry = _normalized(str(item["normalized_content"]))
            if entry not in live:
                continue
            pending.append(
                {
                    "entry": str(item["normalized_content"]),
                    "candidate_id": str(item["id"]),
                    "evidence_event_id": str(item["evidence_quote"]),
                    "detection_event_id": str(item["source_event_id"]),
                }
            )
        if pending:
            return pending
        # Legacy fallback: detections recorded before the candidate ledger
        # (schema v8 era) surfaced as state events only.
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

    def invalidated_closures(self, *, branch_id: str = "main") -> list[dict[str, Any]]:
        """Bidirectional reconciliation, evidence side: an applied closure
        whose file evidence no longer exists inside the hygiene window is
        auto-reverted — the system catches its own mistake, not the user.
        This MUTATES (undo) and therefore runs from reconcile(), the write
        path; hygiene_findings() only reads the resulting revert events."""
        reverted: list[dict[str, Any]] = []
        root = self.service.project_root.resolve()
        now = datetime.now(UTC)
        window = _default_age_days() * 86400
        for item in self.store.list_settlement_candidates(
            kind="task_closure", status="applied"
        ):
            if not str(item.get("evidence_zone", "")).endswith(":strong"):
                continue
            applied_at = str(item.get("status_at") or item["created_at"])
            try:
                age = (now - datetime.fromisoformat(applied_at)).total_seconds()
            except ValueError:
                continue
            if age > window:
                continue
            try:
                detection = self.store.get_event(str(item["source_event_id"]))
            except KeyError:
                continue
            path = str(detection.payload.get("evidence_detail", ""))
            if not path:
                continue
            # Probe the file the captured evidence actually named (host
            # paths are absolute); fall back to the entry path under the
            # project root. Outside-root paths are skipped, not probed (L3).
            probe = None
            try:
                evidence = self.store.get_event(
                    str(detection.payload.get("evidence_event_id", ""))
                )
            except KeyError:
                evidence = None
            if evidence is not None:
                probe = next(
                    (str(f) for f in evidence.files if _path_matches(path, str(f))),
                    None,
                )
            if probe is not None and Path(probe).is_absolute():
                candidate_path = Path(probe).resolve()
            else:
                candidate_path = (root / (probe or path).replace("\\", "/")).resolve()
            if root not in candidate_path.parents and candidate_path != root:
                continue
            if candidate_path.exists():
                continue
            result = self.undo_closure(
                str(item["id"]), branch_id=branch_id,
                rule_id="closure_evidence_invalidated",
                detail={"path": path},
            )
            reverted.append({**result, "path": path})
        return reverted

    def hygiene_findings(self, *, branch_id: str = "main") -> list[dict[str, str]]:
        """Warning-only, recomputed on demand, ranking-neutral — the same
        contract as staleness. Strictly read-only: the invalidation sweep
        itself runs in reconcile(); here we report its revert events while
        they are inside the hygiene window."""
        findings: list[dict[str, str]] = []
        blocks = self.store.get_active_blocks(branch_id=branch_id)
        now = datetime.now(UTC)
        age_days = _default_age_days()
        for event in self.store.events_by_operation(
            "task_closure_reverted", branch_id=branch_id
        ):
            if event.payload.get("rule_id") != "closure_evidence_invalidated":
                continue
            try:
                created = datetime.fromisoformat(event.created_at)
            except ValueError:
                continue
            if (now - created).total_seconds() > age_days * 86400:
                continue
            findings.append(
                {
                    "finding": "closure_evidence_invalidated",
                    "entry": str(event.payload.get("entry", "")),
                    "path": str(event.payload.get("path", "")),
                }
            )
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
