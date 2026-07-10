from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .models import MemoryRecord
from .storage import MemoryStore


@dataclass(frozen=True, slots=True)
class MemoryStaleness:
    memory_id: str
    status: str
    files: tuple[str, ...]
    commits_since: dict[str, int]
    reasons: tuple[str, ...]


class StalenessService:
    def __init__(
        self,
        store: MemoryStore,
        project_root: str | Path,
        *,
        timeout_seconds: float = 2.0,
        default_threshold: int = 3,
    ) -> None:
        self.store = store
        self.project_root = Path(project_root).resolve()
        self.timeout_seconds = timeout_seconds
        self.default_threshold = default_threshold

    def _git(
        self, arguments: Sequence[str]
    ) -> tuple[str | None, str | None]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.project_root), *arguments],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return None, "git executable is unavailable"
        except subprocess.TimeoutExpired:
            return None, "git command timed out"
        except OSError as exc:
            return None, f"git command failed: {exc}"
        if completed.returncode != 0:
            detail = next(
                (
                    line.strip()
                    for line in completed.stderr.splitlines()
                    if line.strip()
                ),
                f"exit {completed.returncode}",
            )
            return None, f"git command failed: {detail}"
        return completed.stdout, None

    def _repository_error(self, cache: dict[tuple[Any, ...], Any]) -> str | None:
        key = ("repository",)
        if key not in cache:
            output, error = self._git(("rev-parse", "--is-inside-work-tree"))
            if error is None and (output or "").strip().casefold() != "true":
                error = "project root is not a Git work tree"
            cache[key] = error
        return cache[key]

    def _relative_file(self, value: str) -> tuple[Path | None, str | None]:
        candidate = Path(value)
        try:
            target = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self.project_root / candidate).resolve()
            )
            relative = target.relative_to(self.project_root)
        except (OSError, RuntimeError):
            return None, f"{value}: path cannot be resolved"
        except ValueError:
            return None, f"{value}: path is outside the project root"
        return relative, None

    def _commit_count(
        self,
        file: str,
        baseline: str,
        cache: dict[tuple[Any, ...], Any],
    ) -> tuple[int | None, str | None]:
        key = ("commits", file, baseline)
        if key in cache:
            return cache[key]
        repository_error = self._repository_error(cache)
        if repository_error is not None:
            result = (None, repository_error)
        else:
            output, error = self._git(
                ("log", "--format=%H", f"--since={baseline}", "--", file)
            )
            result = (
                (len([line for line in (output or "").splitlines() if line.strip()]), None)
                if error is None
                else (None, error)
            )
        cache[key] = result
        return result

    def _baseline(self, record: MemoryRecord) -> tuple[str | None, str | None]:
        parsed: list[datetime] = []
        for event_id in record.source_event_ids:
            event = self.store.get_event(event_id)
            try:
                timestamp = datetime.fromisoformat(event.created_at)
            except ValueError:
                return None, f"{event_id}: source timestamp is unparseable"
            if timestamp.tzinfo is None:
                return None, f"{event_id}: source timestamp has no timezone"
            parsed.append(timestamp)
        if not parsed:
            return None, "memory has no source events"
        return min(parsed).isoformat(), None

    def _inspect_record(
        self,
        record: MemoryRecord,
        *,
        threshold: int,
        cache: dict[tuple[Any, ...], Any],
    ) -> MemoryStaleness:
        commits_since: dict[str, int] = {}
        reasons: list[str] = []
        missing = False
        unknown = False
        stale = False
        baseline, baseline_error = self._baseline(record)
        if baseline_error is not None:
            unknown = True
            reasons.append(baseline_error)

        for file in record.files:
            relative, path_error = self._relative_file(file)
            if path_error is not None:
                unknown = True
                reasons.append(path_error)
                continue
            assert relative is not None
            relative_text = relative.as_posix()
            if not (self.project_root / relative).is_file():
                missing = True
                reasons.append(f"{file}: referenced file is missing")
                continue
            if baseline is None:
                continue
            count, git_error = self._commit_count(relative_text, baseline, cache)
            if git_error is not None:
                unknown = True
                reasons.append(f"{file}: {git_error}")
                continue
            assert count is not None
            commits_since[file] = count
            if count >= threshold:
                stale = True
                reasons.append(
                    f"{file}: {count} commits since source baseline (threshold {threshold})"
                )
            else:
                reasons.append(
                    f"{file}: {count} commits since source baseline"
                )

        status = (
            "missing_file"
            if missing
            else "unknown"
            if unknown
            else "possibly_stale"
            if stale
            else "current"
        )
        return MemoryStaleness(
            memory_id=record.id,
            status=status,
            files=record.files,
            commits_since=commits_since,
            reasons=tuple(reasons),
        )

    def inspect(
        self,
        *,
        branch_id: str = "main",
        memory_id: str | None = None,
        file: str | None = None,
        threshold: int | None = None,
        memory_ids: Sequence[str] = (),
    ) -> tuple[MemoryStaleness, ...]:
        selected_threshold = self.default_threshold if threshold is None else threshold
        if selected_threshold < 1:
            raise ValueError("staleness threshold must be at least 1")
        records = self.store.list_memories(branch_id=branch_id, file=file)
        selected_ids = set(memory_ids)
        if memory_id is not None:
            selected_ids.add(memory_id)
        if selected_ids:
            records = [record for record in records if record.id in selected_ids]
        records = [record for record in records if record.files]
        cache: dict[tuple[Any, ...], Any] = {}
        return tuple(
            self._inspect_record(record, threshold=selected_threshold, cache=cache)
            for record in sorted(records, key=lambda item: item.id)
        )