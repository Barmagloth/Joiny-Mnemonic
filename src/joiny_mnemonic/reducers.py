from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .models import Event
from .prompt import conservative_token_estimate


_ANSI = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_FAILURE = re.compile(
    r"(?i)(?:\bFAIL(?:ED|URE)?\b|\bERROR\b|\bFATAL\b|\bPANIC\b|"
    r"AssertionError|Traceback|Exception|segmentation fault|npm ERR!)"
)
_WARNING = re.compile(r"(?i)(?:\bWARN(?:ING)?\b|deprecated|flaky|retry)")
_SUMMARY = re.compile(
    r"(?i)(?:^=+\s*\d+\s+(?:passed|failed|error|skipped)|"
    r"^\d+\s+(?:passed|failed|skipped|errors?)(?:\s|,|$)|"
    r"^tests?\s+(?:run|passed|failed)|test result:|finished in)"
)
_PATH_LINE = re.compile(r"(?:^|\s)([^\s:]+\.(?:py|js|ts|tsx|rs|go|java|cs|cpp|c|h)):(\d+)")


@dataclass(frozen=True, slots=True)
class ReducedView:
    level: str
    content: str
    strategy: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReductionBundle:
    event_id: str
    family: str
    raw_tokens: int
    raw_bytes: int
    latency_ns: int
    views: tuple[ReducedView, ...]
    critical_signal_count: int
    compact_critical_recall: float

    @property
    def compact(self) -> ReducedView | None:
        return next((view for view in self.views if view.level == "compact"), None)


class ToolOutputReducer:
    """Deterministic, command-aware tool-output reduction.

    Canonical output is never modified. A view is emitted only when its complete prompt
    representation is smaller than the source. High-risk source and diff output pass through.
    """

    name = "builtin-command-aware"
    version = "1"

    def __init__(
        self,
        *,
        minimum_tokens: int = 160,
        head_lines: int = 24,
        tail_lines: int = 24,
        context_lines: int = 2,
    ) -> None:
        self.minimum_tokens = minimum_tokens
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.context_lines = context_lines

    @staticmethod
    def _command(event: Event) -> str:
        value = event.payload.get("tool_input", {})
        if isinstance(value, dict):
            for key in ("command", "cmd", "script", "query", "path", "file_path"):
                if value.get(key):
                    return str(value[key])
        if isinstance(value, str):
            return value
        return " ".join(
            str(event.payload.get(key, ""))
            for key in ("tool_name", "tool", "name")
        ).strip()

    @classmethod
    def family(cls, event: Event) -> str:
        benchmark = event.payload.get("benchmark")
        if isinstance(benchmark, dict) and benchmark.get("family") in {
            "test", "diff", "status", "search", "source", "build", "generic"
        }:
            return str(benchmark["family"])
        command = cls._command(event).casefold()
        if any(item in command for item in ("pytest", "unittest", "cargo test", "go test", "npm test", "pnpm test", "yarn test", "dotnet test")):
            return "test"
        if "git diff" in command or "git show" in command:
            return "diff"
        if "git status" in command:
            return "status"
        if any(item in command for item in ("rg ", "grep ", "findstr", "select-string")):
            return "search"
        if any(item in command for item in ("cat ", "get-content", "read_file", "read file")):
            return "source"
        if any(item in command for item in ("npm install", "pnpm install", "cargo build", "cargo check", "compile", "docker build", "pip install")):
            return "build"
        return "generic"

    @staticmethod
    def _clean(text: str) -> str:
        text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()

    @staticmethod
    def critical_signals(text: str, family: str) -> set[str]:
        signals: set[str] = set()
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line:
                continue
            if family in {"search", "diff"}:
                match = _PATH_LINE.search(line)
                if match:
                    signals.add(f"{match.group(1).casefold()}:{match.group(2)}")
                    continue
            if _FAILURE.search(line) or _SUMMARY.search(line):
                signals.add(line.casefold())
        return signals

    @staticmethod
    def _selected_with_context(lines: list[str], predicate: Any, radius: int) -> list[str]:
        selected: set[int] = set()
        for index, line in enumerate(lines):
            if predicate(line):
                selected.update(range(max(0, index - radius), min(len(lines), index + radius + 1)))
        return [line for index, line in enumerate(lines) if index in selected]

    def _test_view(self, lines: list[str]) -> tuple[str, str, dict[str, Any]]:
        failures = self._selected_with_context(lines, _FAILURE.search, self.context_lines)
        summaries = [line for line in lines if _SUMMARY.search(line)]
        warnings = [line for line in lines if _WARNING.search(line)][-12:]
        selected: list[str] = []
        for section in (failures, warnings, summaries):
            for line in section:
                if line not in selected:
                    selected.append(line)
        if not selected:
            selected = lines[-self.tail_lines :]
        omitted = max(0, len(lines) - len(selected))
        return (
            "\n".join(selected),
            "test-signal-extraction",
            {"raw_lines": len(lines), "kept_lines": len(selected), "omitted_lines": omitted},
        )

    def _search_view(self, lines: list[str]) -> tuple[str, str, dict[str, Any]]:
        references: list[str] = []
        unparsed: list[str] = []
        for line in lines:
            matches = list(_PATH_LINE.finditer(line))
            if matches:
                for match in matches:
                    reference = f"{match.group(1)}:{match.group(2)}"
                    if reference not in references:
                        references.append(reference)
            elif _FAILURE.search(line) or _WARNING.search(line):
                unparsed.append(line)
        selected = [*references, *unparsed]
        return (
            "\n".join(selected),
            "search-reference-index",
            {
                "raw_lines": len(lines),
                "kept_lines": len(selected),
                "path_references": len(references),
                "omitted_lines": max(0, len(lines) - len(selected)),
            },
        )

    def _build_view(self, lines: list[str]) -> tuple[str, str, dict[str, Any]]:
        selected = self._selected_with_context(
            lines, lambda line: bool(_FAILURE.search(line) or _WARNING.search(line)), self.context_lines
        )
        tail = lines[-self.tail_lines :]
        for line in tail:
            if line not in selected:
                selected.append(line)
        return (
            "\n".join(selected),
            "build-signal-and-tail",
            {"raw_lines": len(lines), "kept_lines": len(selected), "omitted_lines": max(0, len(lines) - len(selected))},
        )

    def _generic_view(self, lines: list[str]) -> tuple[str, str, dict[str, Any]]:
        deduplicated: list[str] = []
        duplicate_count = 0
        previous: str | None = None
        repeat_count = 0
        for line in lines:
            if line == previous:
                repeat_count += 1
                duplicate_count += 1
                continue
            if repeat_count and deduplicated:
                deduplicated.append(f"[previous line repeated {repeat_count} more times]")
            deduplicated.append(line)
            previous = line
            repeat_count = 0
        if repeat_count and deduplicated:
            deduplicated.append(f"[previous line repeated {repeat_count} more times]")
        if len(deduplicated) <= self.head_lines + self.tail_lines:
            selected = deduplicated
        else:
            salient = self._selected_with_context(
                deduplicated,
                lambda line: bool(_FAILURE.search(line) or _WARNING.search(line)),
                self.context_lines,
            )
            selected = deduplicated[: self.head_lines]
            selected.append(f"[... {len(deduplicated) - self.head_lines - self.tail_lines} middle lines omitted ...]")
            for line in salient:
                if line not in selected:
                    selected.append(line)
            selected.extend(deduplicated[-self.tail_lines :])
        return (
            "\n".join(selected),
            "deduplicate-salient-head-tail",
            {
                "raw_lines": len(lines),
                "kept_lines": len(selected),
                "duplicate_lines_collapsed": duplicate_count,
                "omitted_lines": max(0, len(lines) - len(selected)),
            },
        )

    @staticmethod
    def _summary(compact: str, family: str, metadata: dict[str, Any]) -> str:
        lines = compact.splitlines()
        salient = [line for line in lines if _FAILURE.search(line) or _SUMMARY.search(line)]
        if not salient:
            salient = lines[:8] + (lines[-4:] if len(lines) > 12 else [])
        header = (
            f"family={family}; raw_lines={metadata.get('raw_lines', len(lines))}; "
            f"kept_lines={metadata.get('kept_lines', len(lines))}; "
            f"omitted_lines={metadata.get('omitted_lines', 0)}"
        )
        return "\n".join([header, *salient[:24]])

    @staticmethod
    def _frame(event_id: str, level: str, strategy: str, raw_tokens: int, content: str) -> str:
        return (
            f"[tool-output-view source={event_id} level={level} strategy={strategy} raw_tokens={raw_tokens}]\n"
            f"{content}\n"
            f"[exact source available: {event_id}]"
        )

    def reduce(self, event: Event) -> ReductionBundle:
        if event.kind != "tool_output":
            raise ValueError("only tool_output events can be reduced")
        started = time.perf_counter_ns()
        raw = event.content
        raw_bytes = len(raw.encode("utf-8"))
        raw_tokens = conservative_token_estimate(raw)
        family = self.family(event)
        cleaned = self._clean(raw)
        lines = cleaned.splitlines()
        critical = self.critical_signals(raw, family)
        views: list[ReducedView] = []

        if raw_tokens >= self.minimum_tokens and family not in {"source", "diff", "status"}:
            if family == "test":
                compact_body, strategy, metadata = self._test_view(lines)
            elif family == "search":
                compact_body, strategy, metadata = self._search_view(lines)
            elif family == "build":
                compact_body, strategy, metadata = self._build_view(lines)
            else:
                compact_body, strategy, metadata = self._generic_view(lines)
            compact = self._frame(event.id, "compact", strategy, raw_tokens, compact_body)
            compact_tokens = conservative_token_estimate(compact)
            retained = self.critical_signals(compact_body, family)
            recall = 1.0 if not critical else len(critical & retained) / len(critical)
            metadata = {
                **metadata,
                "family": family,
                "critical_signal_count": len(critical),
                "critical_signal_recall": recall,
            }
            if compact_tokens < raw_tokens:
                views.append(ReducedView("compact", compact, strategy, metadata))
                summary_body = self._summary(compact_body, family, metadata)
                summary = self._frame(event.id, "summary", strategy, raw_tokens, summary_body)
                if conservative_token_estimate(summary) < compact_tokens:
                    views.append(ReducedView("summary", summary, strategy, metadata))
        else:
            recall = 1.0

        latency_ns = time.perf_counter_ns() - started
        compact_view = next((view for view in views if view.level == "compact"), None)
        if compact_view is not None:
            retained = self.critical_signals(compact_view.content, family)
            recall = 1.0 if not critical else len(critical & retained) / len(critical)
        else:
            recall = 1.0
        return ReductionBundle(
            event_id=event.id,
            family=family,
            raw_tokens=raw_tokens,
            raw_bytes=raw_bytes,
            latency_ns=latency_ns,
            views=tuple(views),
            critical_signal_count=len(critical),
            compact_critical_recall=recall,
        )


def materialize_view(event: Event, reduced: ReducedView, bundle: ReductionBundle) -> dict[str, Any]:
    content_hash = hashlib.sha256(reduced.content.encode("utf-8")).hexdigest()
    return {
        "event_id": event.id,
        "level": reduced.level,
        "reducer": ToolOutputReducer.name,
        "reducer_version": ToolOutputReducer.version,
        "content": reduced.content,
        "source_hash": event.content_hash,
        "content_hash": content_hash,
        "raw_bytes": bundle.raw_bytes,
        "view_bytes": len(reduced.content.encode("utf-8")),
        "raw_tokens": bundle.raw_tokens,
        "view_tokens": conservative_token_estimate(reduced.content),
        "latency_ns": bundle.latency_ns,
        "metadata": reduced.metadata,
    }