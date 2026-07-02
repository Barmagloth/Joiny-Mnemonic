from __future__ import annotations

import hashlib
import os
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .models import Event, Snapshot
from .retrieval import lexical_terms
from .storage import MemoryStore
from .transcript import recent_complete_events


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def fingerprint_project(root: str | Path, files: Sequence[str] | None = None) -> dict[str, Any]:
    root_path = Path(root).resolve()
    head = _git(root_path, "rev-parse", "HEAD")
    selected: list[Path] = []
    if files is not None:
        selected = [root_path / item for item in files]
    else:
        tracked = _git(root_path, "ls-files", "-z")
        if tracked is not None:
            selected = [root_path / item for item in tracked.split("\0") if item]
        else:
            excluded = {".git", ".joiny-mnemonic", ".llm-memory", "__pycache__", ".pytest_cache", ".tmp"}
            for current, directories, filenames in os.walk(root_path, onerror=lambda _: None):
                directories[:] = [item for item in directories if item not in excluded]
                selected.extend(
                    Path(current) / name for name in filenames
                    if not name.endswith((".db", ".db-wal", ".db-shm", ".pyc"))
                )
    hashes: dict[str, str] = {}
    for candidate in selected:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root_path) or not resolved.is_file():
            continue
        hashes[resolved.relative_to(root_path).as_posix()] = _file_hash(resolved)
    return {"root": str(root_path), "git_head": head, "files": dict(sorted(hashes.items()))}


def compare_fingerprints(previous: dict[str, Any], current: dict[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if previous.get("root") != current.get("root"):
        reasons.append("project root changed")
    if previous.get("git_head") != current.get("git_head"):
        reasons.append("Git HEAD changed")
    old_files = previous.get("files", {})
    new_files = current.get("files", {})
    changed = sorted(
        path for path in set(old_files) | set(new_files)
        if old_files.get(path) != new_files.get(path)
    )
    if changed:
        preview = ", ".join(changed[:8])
        if len(changed) > 8:
            preview += f" (+{len(changed) - 8} more)"
        reasons.append(f"file hashes changed: {preview}")
    return tuple(reasons)


@dataclass(frozen=True, slots=True)
class RestoredState:
    snapshot: Snapshot
    state: dict[str, Any]
    replayed_events: tuple[Event, ...]
    stale_reasons: tuple[str, ...]


class SnapshotManager:
    RECENT_GROUP_LIMIT = 32

    def __init__(self, store: MemoryStore, project_root: str | Path) -> None:
        self.store = store
        self.project_root = Path(project_root).resolve()

    def build_state(self, *, branch_id: str = "main") -> dict[str, Any]:
        records = self.store.list_memories(branch_id=branch_id)
        events = self.store.query_events(branch_id=branch_id)
        blocks = {
            name: asdict(block) for name, block in self.store.get_active_blocks(branch_id=branch_id).items()
        }
        memories = {
            record.id: asdict(record) for record in records
        }
        lexical_index: dict[str, list[str]] = defaultdict(list)
        for record in records:
            for term in lexical_terms(
                f"{record.summary}\n{record.content}\n{' '.join(record.files)}"
            ):
                lexical_index[term].append(record.id)
        recent = recent_complete_events(
            events, self.RECENT_GROUP_LIMIT
        )
        return {
            "blocks": blocks,
            "memories": memories,
            "index": {
                record.id: {
                    "id": record.id,
                    "type": record.memory_type,
                    "summary": record.summary,
                    "files": list(record.files),
                    "source_event_ids": list(record.source_event_ids),
                }
                for record in records
            },
            "recent_events": {event.id: event.to_dict() for event in recent},
            "recent_event_order": [event.id for event in recent],
            "timeline_index": {
                event.id: {
                    "seq": event.seq,
                    "id": event.id,
                    "time": event.created_at,
                    "kind": event.kind,
                    "role": event.role,
                    "preview": " ".join(event.content.split())[:160],
                    "files": list(event.files),
                }
                for event in events
            },
            "lexical_index": {
                term: sorted(memory_ids) for term, memory_ids in lexical_index.items()
            },
        }

    def read_project_source(
        self, relative_path: str, *, expected_hash: str | None = None
    ) -> dict[str, Any]:
        """Read exact current source content without allowing project-root escape."""
        path = (self.project_root / relative_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("source path escapes the configured project root")
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        data = path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        return {
            "path": path.relative_to(self.project_root).as_posix(),
            "content": data.decode("utf-8", errors="replace"),
            "content_hash": content_hash,
            "matches_expected_hash": expected_hash is None or content_hash == expected_hash,
        }

    def create(
        self,
        *,
        branch_id: str = "main",
        parent_snapshot_id: str | None = None,
        tracked_files: Sequence[str] | None = None,
    ) -> Snapshot:
        return self.store.create_snapshot(
            state=self.build_state(branch_id=branch_id),
            project=fingerprint_project(self.project_root, tracked_files),
            branch_id=branch_id,
            parent_snapshot_id=parent_snapshot_id,
        )

    @staticmethod
    def _replay(state: dict[str, Any], events: Sequence[Event]) -> dict[str, Any]:
        raw_index = state.get("index", {})
        if isinstance(raw_index, list):
            raw_index = {item["id"]: item for item in raw_index}
        result = {
            "blocks": dict(state.get("blocks", {})),
            "memories": dict(state.get("memories", {})),
            "index": dict(raw_index),
            "recent_events": dict(state.get("recent_events", {})),
            "recent_event_order": list(state.get("recent_event_order", [])),
            "timeline_index": dict(state.get("timeline_index", {})),
            "lexical_index": dict(state.get("lexical_index", {})),
        }
        for event in events:
            result["timeline_index"][event.id] = {
                "seq": event.seq,
                "id": event.id,
                "time": event.created_at,
                "kind": event.kind,
                "role": event.role,
                "preview": " ".join(event.content.split())[:160],
                "files": list(event.files),
            }
            if event.kind in {"message", "tool_call", "tool_output", "artifact"}:
                result["recent_events"][event.id] = event.to_dict()
                if event.id not in result["recent_event_order"]:
                    result["recent_event_order"].append(event.id)
            if event.kind == "memory_block":
                name = event.payload.get("block")
                if name:
                    previous = result["blocks"].get(name, {})
                    result["blocks"][name] = {
                        "id": f"replay:{event.id}",
                        "branch_id": event.branch_id,
                        "name": name,
                        "content": event.content,
                        "version": int(previous.get("version", 0)) + 1,
                        "source_event_ids": [event.id],
                        "supersedes_id": previous.get("id"),
                        "created_at": event.created_at,
                    }
            elif event.kind == "state" and event.payload.get("operation") == "derive_memory":
                item = dict(event.payload)
                item["derivation_event_id"] = event.id
                memory_id = item.get("memory_id", f"replay:{event.id}")
                supersedes_id = item.get("supersedes_id")
                if supersedes_id:
                    result["memories"].pop(supersedes_id, None)
                    result["index"].pop(supersedes_id, None)
                item.setdefault("id", memory_id)
                item.setdefault("branch_id", event.branch_id)
                item.setdefault("created_at", event.created_at)
                item.setdefault("version", 1)
                result["memories"][memory_id] = item
                result["index"][memory_id] = {
                    "id": memory_id,
                    "type": item.get("memory_type"),
                    "summary": item.get("summary"),
                    "files": item.get("files", []),
                    "source_event_ids": item.get("source_event_ids", []),
                }
        recent_objects: list[Event] = []
        for event_id in result["recent_event_order"]:
            raw = result["recent_events"].get(event_id)
            if not raw:
                continue
            recent_objects.append(
                Event(
                    seq=int(raw["seq"]), id=raw["id"], branch_id=raw["branch_id"],
                    session_id=raw.get("session_id"), kind=raw["kind"], role=raw.get("role"),
                    content=raw["content"], payload=dict(raw.get("payload", {})),
                    files=tuple(raw.get("files", ())), created_at=raw["created_at"],
                    previous_hash=raw.get("previous_hash"), content_hash=raw["content_hash"],
                    chain_hash=raw["chain_hash"],
                )
            )
        recent_objects = recent_complete_events(
            recent_objects, SnapshotManager.RECENT_GROUP_LIMIT
        )
        result["recent_events"] = {event.id: event.to_dict() for event in recent_objects}
        result["recent_event_order"] = [event.id for event in recent_objects]
        rebuilt_index: dict[str, list[str]] = defaultdict(list)
        for memory_id, item in result["memories"].items():
            for term in lexical_terms(
                f"{item.get('summary', '')}\n{item.get('content', '')}\n"
                f"{' '.join(item.get('files', []))}"
            ):
                rebuilt_index[term].append(memory_id)
        result["lexical_index"] = {
            term: sorted(memory_ids) for term, memory_ids in rebuilt_index.items()
        }
        return result

    def restore(self, snapshot_id: str, *, branch_id: str | None = None) -> RestoredState:
        snapshot = self.store.get_snapshot(snapshot_id)
        tail = self.store.snapshot_tail(snapshot_id, target_branch_id=branch_id)
        current = fingerprint_project(
            self.project_root,
            list(snapshot.project.get("files", {}).keys()),
        )
        return RestoredState(
            snapshot=snapshot,
            state=self._replay(snapshot.state, tail),
            replayed_events=tuple(tail),
            stale_reasons=compare_fingerprints(snapshot.project, current),
        )
