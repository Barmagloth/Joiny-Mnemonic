from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .models import Event
from .prompt import conservative_token_estimate


def _env_protected_patterns() -> tuple[str, ...]:
    raw = os.environ.get("JOINY_MNEMONIC_PROTECTED_PATTERNS", "")
    return tuple(item.strip() for item in raw.split(";") if item.strip())


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


def first_failure_line(value: str) -> str | None:
    cleaned = _ANSI.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    for raw in cleaned.splitlines():
        line = " ".join(raw.split())
        if line and _FAILURE.search(line):
            return line[:240]
    return None


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
        protected_patterns: Sequence[str] | None = None,
    ) -> None:
        self.minimum_tokens = minimum_tokens
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.context_lines = context_lines
        # task5.md D4 (Headroom audit_safe, Apache-2.0): content matching any
        # of these regexes must survive every reduction verbatim — not
        # dropped, not summarized away. If that cannot be guaranteed, the
        # reducer fails closed and emits no view: compression may never eat
        # the compliance row.
        patterns = (
            tuple(protected_patterns)
            if protected_patterns is not None
            else _env_protected_patterns()
        )
        self.protected = tuple(re.compile(item) for item in patterns)

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

    # --- JSON-array view (task5.md D3; SmartCrusher recipe from Headroom,
    # Apache-2.0: first 30% + last 15% + change-points + dedup, cap 15;
    # lossless tabular preferred at a 15% savings gate because it needs no
    # retrieval round-trip; the drop sentinel sits IN the array, at the
    # site of the missing rows) -------------------------------------------

    _JSON_MIN_ROWS = 5
    _JSON_CAP = 15
    _JSON_FIRST = 0.30
    _JSON_LAST = 0.15
    _JSON_LOSSLESS_GATE = 0.15
    _JSON_CORE_FIELD_FRACTION = 0.8

    @staticmethod
    def _parse_json_rows(raw: str) -> list[dict[str, Any]] | None:
        text = raw.strip()
        if not text.startswith(("[", "{")):
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(value, dict):
            # Common single-wrapper shapes: {"items": [...]}, {"results": [...]}.
            lists = [item for item in value.values() if isinstance(item, list)]
            if len(lists) == 1:
                value = lists[0]
        if not isinstance(value, list):
            return None
        if not all(isinstance(item, dict) for item in value):
            return None
        return value

    def _row_protected(self, row: dict[str, Any]) -> bool:
        if not self.protected:
            return False
        canonical = json.dumps(row, ensure_ascii=False, sort_keys=True)
        return any(pattern.search(canonical) for pattern in self.protected)

    @staticmethod
    def _lossless_csv(rows: list[dict[str, Any]], core: list[str]) -> str | None:
        rendered = [",".join(core)]
        for row in rows:
            cells = []
            for key in core:
                value = row.get(key, "")
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                cell = str(value)
                if any(ch in cell for ch in ",\"\n"):
                    cell = '"' + cell.replace('"', '""') + '"'
                cells.append(cell)
            extras = {key: value for key, value in row.items() if key not in core}
            if extras:
                return None  # rows outside the core schema: not losslessly tabular
            rendered.append(",".join(cells))
        return "\n".join(rendered)

    def _json_array_view(
        self, rows: list[dict[str, Any]], event_id: str, raw: str
    ) -> tuple[str, str, dict[str, Any]] | None:
        total = len(rows)
        deduplicated: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for row in rows:
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key in seen:
                seen[key] += 1
                continue
            seen[key] = 1
            deduplicated.append(row)

        key_counts: dict[str, int] = {}
        for row in deduplicated:
            for key in row:
                key_counts[key] = key_counts.get(key, 0) + 1
        core = [
            key for key, count in key_counts.items()
            if count / max(len(deduplicated), 1) >= self._JSON_CORE_FIELD_FRACTION
        ]
        if core and len(deduplicated) == total:
            csv_text = self._lossless_csv(deduplicated, core)
            if csv_text is not None:
                savings = 1.0 - len(csv_text.encode()) / max(len(raw.encode()), 1)
                if savings >= self._JSON_LOSSLESS_GATE:
                    return (
                        csv_text,
                        "json-lossless-csv",
                        {
                            "rows_total": total,
                            "rows_kept": total,
                            "rows_dropped": 0,
                            "csv_savings": round(savings, 4),
                        },
                    )

        # The cap outranks the fractions: allocate it 60/40 between the head
        # and the tail, then let schema change-points fill any remaining
        # slack. Protected rows ride above the cap unconditionally.
        count = len(deduplicated)
        first = min(math.ceil(count * self._JSON_FIRST), count)
        last = min(math.ceil(count * self._JSON_LAST), count)
        first_quota = min(first, math.ceil(self._JSON_CAP * 0.6))
        last_quota = min(last, self._JSON_CAP - first_quota)
        kept_indexes: set[int] = set(range(first_quota))
        kept_indexes.update(range(max(0, count - last_quota), count))
        slack = self._JSON_CAP - len(kept_indexes)
        if slack > 0:
            previous_keys: frozenset[str] | None = None
            for index, row in enumerate(deduplicated):
                keys = frozenset(row)
                if (
                    previous_keys is not None
                    and keys != previous_keys
                    and index not in kept_indexes
                ):
                    kept_indexes.add(index)  # schema change-point
                    slack -= 1
                    if slack == 0:
                        break
                previous_keys = keys
        for index, row in enumerate(deduplicated):
            if self._row_protected(row):
                kept_indexes.add(index)

        kept = [deduplicated[index] for index in sorted(kept_indexes)]
        if self.protected:
            for row in deduplicated:
                if self._row_protected(row) and row not in kept:
                    return None  # fail closed: never ship a view missing one
        dropped = total - len(kept)
        body = list(kept)
        if dropped > 0:
            body.append(
                {
                    "_dropped": (
                        f"{dropped} rows omitted; exact source: "
                        f"memory_source {event_id}"
                    )
                }
            )
        return (
            json.dumps(body, ensure_ascii=False),
            "json-array-crush",
            {
                "rows_total": total,
                "rows_kept": len(kept),
                "rows_dropped": dropped,
                "duplicates_collapsed": total - len(deduplicated),
            },
        )

    def _enforce_protected_lines(
        self, compact_body: str, lines: list[str]
    ) -> str | None:
        """Line-based D4: every protected source line must be in the view;
        missing ones are appended in source order, or the view is refused."""
        if not self.protected:
            return compact_body
        required = [
            line for line in lines
            if line and any(pattern.search(line) for pattern in self.protected)
        ]
        if not required:
            return compact_body
        present = set(compact_body.splitlines())
        missing = [line for line in required if line not in present]
        result = compact_body
        if missing:
            result = "\n".join([compact_body, "[protected lines]", *missing])
        for line in required:
            if line not in result:
                return None  # defensive fail-closed
        return result

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
            view: tuple[str, str, dict[str, Any]] | None
            if family == "generic" and (
                (rows := self._parse_json_rows(raw)) is not None
                and len(rows) >= self._JSON_MIN_ROWS
            ):
                family = "json"
                view = self._json_array_view(rows, event.id, raw)
            elif family == "test":
                view = self._test_view(lines)
            elif family == "search":
                view = self._search_view(lines)
            elif family == "build":
                view = self._build_view(lines)
            else:
                view = self._generic_view(lines)
            if view is not None and family != "json":
                enforced = self._enforce_protected_lines(view[0], lines)
                view = None if enforced is None else (enforced, view[1], view[2])
            if view is None:
                # Fail closed (task5.md D4): a protected row/line could not
                # be guaranteed in the view; the raw output stands alone.
                return ReductionBundle(
                    event_id=event.id, family=family, raw_tokens=raw_tokens,
                    raw_bytes=raw_bytes,
                    latency_ns=time.perf_counter_ns() - started, views=(),
                    critical_signal_count=len(critical),
                    compact_critical_recall=1.0,
                )
            compact_body, strategy, metadata = view
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