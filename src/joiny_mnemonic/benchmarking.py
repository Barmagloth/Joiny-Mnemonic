from __future__ import annotations

import hashlib
import contextlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import uuid
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .models import Event
from .prompt import conservative_token_estimate
from .reducers import ToolOutputReducer
from .service import MemoryService


_PATH_REFERENCE = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.(?:py|js|ts|tsx|rs|go|java|cs|cpp|c|h)):(?P<line>\d+)"
)


@dataclass(frozen=True, slots=True)
class BenchmarkWorkload:
    name: str
    argv: tuple[str, ...]
    family: str
    expected_returncodes: tuple[int, ...] = (0,)
    cwd: str = "."


@dataclass(frozen=True, slots=True)
class CommandCapture:
    workload: BenchmarkWorkload
    output: str
    returncode: int
    duration_ms: float


class TokenCounter:
    def __init__(self, model: str = "gpt-4o") -> None:
        self.model = model
        self.exact = False
        self.backend = "conservative-byte-word-estimate"
        self._count: Callable[[str], int] = conservative_token_estimate
        try:
            import tiktoken  # type: ignore

            try:
                encoding = tiktoken.encoding_for_model(model)
            except KeyError:
                encoding = tiktoken.get_encoding("o200k_base")
            self._count = lambda value: len(encoding.encode(value))
            self.backend = f"tiktoken:{encoding.name}"
            self.exact = True
        except ImportError:
            pass

    def __call__(self, value: str) -> int:
        return self._count(value)


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def _line_set(text: str) -> set[str]:
    return {" ".join(line.split()) for line in text.splitlines() if line.strip()}


def _path_references(text: str) -> set[str]:
    return {
        f"{match.group('path').replace('\\', '/').casefold()}:{match.group('line')}"
        for match in _PATH_REFERENCE.finditer(text)
    }


def _recall(expected: set[str], actual: set[str]) -> float:
    return 1.0 if not expected else len(expected & actual) / len(expected)


def default_workloads(project_root: str | Path) -> tuple[BenchmarkWorkload, ...]:
    root = Path(project_root).resolve()
    workloads = [
        BenchmarkWorkload(
            "project-test-suite",
            (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"),
            "test",
        ),
        BenchmarkWorkload(
            "controlled-failing-suite",
            (
                sys.executable, "-m", "unittest", "discover", "-s",
                "benchmarks/fixtures/failing_suite", "-v",
            ),
            "test",
            expected_returncodes=(1,),
        ),
    ]
    if shutil.which("rg"):
        workloads.append(
            BenchmarkWorkload(
                "real-source-search",
                ("rg", "-n", "def |class ", "src/joiny_mnemonic", "tests"),
                "search",
            )
        )
    if shutil.which("git"):
        workloads.append(
            BenchmarkWorkload(
                "real-git-diff-no-index",
                (
                    "git", "diff", "--no-index", "--",
                    "benchmarks/fixtures/diff_before.py",
                    "benchmarks/fixtures/diff_after.py",
                ),
                "diff",
                expected_returncodes=(1,),
            )
        )
    return tuple(workloads)


def capture_workload(workload: BenchmarkWorkload, project_root: Path) -> CommandCapture:
    env = dict(os.environ)
    source_path = str(project_root / "src")
    env["PYTHONPATH"] = source_path + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.perf_counter()
    result = subprocess.run(
        workload.argv,
        cwd=project_root / workload.cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    duration_ms = (time.perf_counter() - started) * 1000
    if result.returncode not in workload.expected_returncodes:
        raise RuntimeError(
            f"workload {workload.name} returned {result.returncode}; expected "
            f"{workload.expected_returncodes}\n{result.stdout}\n{result.stderr}"
        )
    output = result.stdout
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    return CommandCapture(workload, output, result.returncode, duration_ms)


def _event_for_output(service: MemoryService, capture: CommandCapture) -> Event:
    command = subprocess.list2cmdline(list(capture.workload.argv))
    return service.store.append_event(
        kind="tool_output",
        role="tool",
        content=capture.output,
        payload={
            "tool_input": {"command": command},
            "benchmark": {"workload": capture.workload.name, "family": capture.workload.family},
        },
    )


def run_benchmark(
    project_root: str | Path,
    *,
    reduction_repetitions: int = 100,
    prompt_exposures: int = 10,
    token_model: str = "gpt-4o",
    workloads: Sequence[BenchmarkWorkload] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    selected = tuple(workloads or default_workloads(root))
    captures = [capture_workload(workload, root) for workload in selected]
    counter = TokenCounter(token_model)
    reducer = ToolOutputReducer()

    rows: list[dict[str, Any]] = []
    reducer_latencies: list[float] = []
    baseline_ingest: list[float] = []
    enriched_ingest: list[float] = []
    promotion_latencies: list[float] = []
    hook_counter_latencies: list[float] = []

    runtime_root = root / "benchmarks" / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    with contextlib.nullcontext(runtime_root) as directory:
        temporary = Path(directory)
        baseline = MemoryService(temporary / f"baseline-{run_id}.db", project_root=root)
        enriched = MemoryService(temporary / f"enriched-{run_id}.db", project_root=root)
        try:
            for capture in captures:
                baseline_started = time.perf_counter_ns()
                _event_for_output(baseline, capture)
                baseline_ingest.append((time.perf_counter_ns() - baseline_started) / 1_000_000)

                enriched_started = time.perf_counter_ns()
                event = _event_for_output(enriched, capture)
                bundle, stored_views = enriched.reduce_tool_output(event)
                enriched_ingest.append((time.perf_counter_ns() - enriched_started) / 1_000_000)
                compact = next((view for view in stored_views if view.level == "compact"), None)
                emitted = compact.content if compact is not None else capture.output

                pure_latencies: list[float] = []
                for _ in range(reduction_repetitions):
                    started = time.perf_counter_ns()
                    reducer.reduce(event)
                    pure_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
                reducer_latencies.extend(pure_latencies)

                raw_lines = _line_set(capture.output)
                emitted_lines = _line_set(emitted)
                raw_paths = _path_references(capture.output)
                emitted_paths = _path_references(emitted)
                raw_critical = reducer.critical_signals(capture.output, capture.workload.family)
                emitted_critical = reducer.critical_signals(emitted, capture.workload.family)

                promote_started = time.perf_counter_ns()
                restored = enriched.exact_source(compact.id if compact is not None else event.id)[0].content
                promotion_ms = (time.perf_counter_ns() - promote_started) / 1_000_000
                promotion_latencies.append(promotion_ms)
                exact = hashlib.sha256(restored.encode()).digest() == hashlib.sha256(capture.output.encode()).digest()

                raw_tokens = counter(capture.output)
                emitted_tokens = counter(emitted)
                saved = raw_tokens - emitted_tokens
                rows.append(
                    {
                        "workload": capture.workload.name,
                        "family": capture.workload.family,
                        "command": list(capture.workload.argv),
                        "command_duration_ms": capture.duration_ms,
                        "returncode": capture.returncode,
                        "raw_bytes": len(capture.output.encode("utf-8")),
                        "emitted_bytes": len(emitted.encode("utf-8")),
                        "raw_tokens": raw_tokens,
                        "emitted_tokens": emitted_tokens,
                        "tokens_saved_per_exposure": saved,
                        "savings_ratio": saved / raw_tokens if raw_tokens else 0.0,
                        "view_emitted": compact is not None,
                        "strategy": compact.reducer if compact is not None else "canonical-raw",
                        "critical_signal_recall": _recall(raw_critical, emitted_critical),
                        "path_reference_recall": _recall(raw_paths, emitted_paths),
                        "raw_path_reference_count": len(raw_paths),
                        "emitted_path_reference_count": len(raw_paths & emitted_paths),
                        "verbatim_line_recall": _recall(raw_lines, emitted_lines),
                        "raw_line_count": len(raw_lines),
                        "emitted_raw_line_count": len(raw_lines & emitted_lines),
                        "exact_source_recoverable": exact,
                        "source_promotion_ms": promotion_ms,
                        "reducer_latency_ms_p50": statistics.median(pure_latencies),
                        "reducer_latency_ms_p95": percentile(pure_latencies, 0.95),
                    }
                )

            baseline.store.checkpoint()
            enriched.store.checkpoint()
            baseline_bytes = baseline.store.database_size()
            enriched_bytes = enriched.store.database_size()
            usage = enriched.usage.report()
        finally:
            baseline.close()
            enriched.close()

    hook_service = MemoryService(runtime_root / f"hook-counter-{run_id}.db", project_root=root)
    try:
        hook_session = hook_service.store.start_session("benchmark-hook-counter")
        hook_event = hook_service.store.append_event(
            kind="message",
            role="user",
            content="benchmark hook context payload " * 32,
            session_id=hook_session,
        )
        hook_counter_repetitions = max(20, reduction_repetitions)
        last_counter = None
        for index in range(hook_counter_repetitions):
            started = time.perf_counter_ns()
            last_counter = hook_service.usage.record_hook_context(
                [hook_event],
                event_name="UserPromptSubmit",
                branch_id="main",
                session_id=hook_session,
                receipt_key=f"benchmark-hook:{index}",
                context_window_tokens=200_000,
                threshold_tokens=90_000,
            )
            hook_counter_latencies.append(
                (time.perf_counter_ns() - started) / 1_000_000
            )
        assert last_counter is not None
        hook_counter_total = hook_service.store.hook_context_total(
            branch_id="main", session_id=hook_session
        )
        hook_counter_expected = last_counter.increment_tokens * hook_counter_repetitions
        hook_service.usage.record_hook_context(
            [hook_event],
            event_name="UserPromptSubmit",
            branch_id="main",
            session_id=hook_session,
            receipt_key="benchmark-hook:0",
            context_window_tokens=200_000,
            threshold_tokens=90_000,
        )
        hook_counter_replay_total = hook_service.store.hook_context_total(
            branch_id="main", session_id=hook_session
        )
    finally:
        hook_service.close()
    raw_tokens = sum(row["raw_tokens"] for row in rows)
    emitted_tokens = sum(row["emitted_tokens"] for row in rows)
    saved_once = raw_tokens - emitted_tokens
    total_reducer_ms = sum(reducer_latencies) / max(1, reduction_repetitions)
    storage_overhead = enriched_bytes - baseline_bytes
    critical_expected = sum(
        len(reducer.critical_signals(capture.output, capture.workload.family))
        for capture in captures
    )
    critical_retained = sum(
        round(len(reducer.critical_signals(capture.output, capture.workload.family)) * row["critical_signal_recall"])
        for capture, row in zip(captures, rows)
    )
    raw_line_count = sum(row["raw_line_count"] for row in rows)
    retained_line_count = sum(row["emitted_raw_line_count"] for row in rows)
    raw_path_count = sum(row["raw_path_reference_count"] for row in rows)
    retained_path_count = sum(row["emitted_path_reference_count"] for row in rows)
    aggregate = {
        "workloads": len(rows),
        "raw_tokens": raw_tokens,
        "emitted_tokens": emitted_tokens,
        "tokens_saved_per_exposure": saved_once,
        "token_savings_ratio": saved_once / raw_tokens if raw_tokens else 0.0,
        "prompt_exposures": prompt_exposures,
        "tokens_saved_at_exposures": saved_once * prompt_exposures,
        "reducer_latency_ms_p50": statistics.median(reducer_latencies) if reducer_latencies else 0.0,
        "reducer_latency_ms_p95": percentile(reducer_latencies, 0.95),
        "reducer_cpu_ms_per_corpus": total_reducer_ms,
        "tokens_saved_per_reducer_ms": saved_once / total_reducer_ms if total_reducer_ms else 0.0,
        "baseline_ingest_ms_p50": statistics.median(baseline_ingest) if baseline_ingest else 0.0,
        "enriched_ingest_ms_p50": statistics.median(enriched_ingest) if enriched_ingest else 0.0,
        "enriched_ingest_ms_p95": percentile(enriched_ingest, 0.95),
        "ingest_overhead_ms_p50": (
            (statistics.median(enriched_ingest) - statistics.median(baseline_ingest))
            if baseline_ingest and enriched_ingest else 0.0
        ),
        "baseline_database_bytes": baseline_bytes,
        "enriched_database_bytes": enriched_bytes,
        "storage_overhead_bytes": storage_overhead,
        "storage_overhead_per_saved_token": storage_overhead / saved_once if saved_once > 0 else None,
        "critical_signal_recall": critical_retained / critical_expected if critical_expected else 1.0,
        "exact_source_recovery_rate": (
            sum(bool(row["exact_source_recoverable"]) for row in rows) / len(rows) if rows else 1.0
        ),
        "source_promotion_ms_p95": percentile(promotion_latencies, 0.95),
        "hook_counter_append_ms_p50": statistics.median(hook_counter_latencies),
        "hook_counter_append_ms_p95": percentile(hook_counter_latencies, 0.95),
        "hook_counter_repetitions": hook_counter_repetitions,
        "hook_counter_cumulative_exact": hook_counter_total == hook_counter_expected,
        "hook_counter_replay_idempotent": hook_counter_replay_total == hook_counter_total,
        "immediate_verbatim_line_recall": (
            retained_line_count / raw_line_count if raw_line_count else 1.0
        ),
        "path_reference_recall": (
            retained_path_count / raw_path_count if raw_path_count else 1.0
        ),
    }
    gates = {
        "positive_net_token_gain": saved_once > 0,
        "critical_signal_recall_100pct": aggregate["critical_signal_recall"] == 1.0,
        "exact_source_recovery_100pct": aggregate["exact_source_recovery_rate"] == 1.0,
        "path_reference_recall_100pct": aggregate["path_reference_recall"] == 1.0,
        "reducer_p95_under_50ms": aggregate["reducer_latency_ms_p95"] < 50.0,
        "enriched_ingest_p95_under_100ms": aggregate["enriched_ingest_ms_p95"] < 100.0,
        "hook_counter_p95_under_25ms": aggregate["hook_counter_append_ms_p95"] < 25.0,
        "hook_counter_cumulative_exact": aggregate["hook_counter_cumulative_exact"],
        "hook_counter_replay_idempotent": aggregate["hook_counter_replay_idempotent"],
        "no_workload_expands_prompt": all(row["tokens_saved_per_exposure"] >= 0 for row in rows),
    }
    return {
        "schema": "joiny-mnemonic-benchmark-v2",
        "project_root": str(root),
        "token_counter": {"backend": counter.backend, "model": token_model, "exact_for_model": counter.exact},
        "parameters": {
            "reduction_repetitions": reduction_repetitions,
            "prompt_exposures": prompt_exposures,
        },
        "aggregate": aggregate,
        "workloads": rows,
        "usage_meter_cross_check": usage,
        "gates": gates,
        "passed": all(gates.values()),
    }


def render_markdown(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    lines = [
        "# Joiny-Mnemonic performance and retention benchmark",
        "",
        f"Token counter: `{report['token_counter']['backend']}`. "
        f"Exact for selected model: `{report['token_counter']['exact_for_model']}`.",
        "",
        "| Workload | Raw tokens | Emitted | Saved | Critical recall | Path refs | Line recall | Exact recovery | Reducer p95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["workloads"]:
        lines.append(
            f"| {row['workload']} | {row['raw_tokens']} | {row['emitted_tokens']} | "
            f"{row['tokens_saved_per_exposure']} | {row['critical_signal_recall']:.1%} | "
            f"{row['path_reference_recall']:.1%} | {row['verbatim_line_recall']:.1%} | "
            f"{'yes' if row['exact_source_recoverable'] else 'NO'} | "
            f"{row['reducer_latency_ms_p95']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Aggregate",
            "",
            f"- Token savings per exposure: **{aggregate['tokens_saved_per_exposure']} "
            f"({aggregate['token_savings_ratio']:.1%})**.",
            f"- Token savings at {aggregate['prompt_exposures']} exposures: "
            f"**{aggregate['tokens_saved_at_exposures']}**.",
            f"- Reducer latency p95: **{aggregate['reducer_latency_ms_p95']:.3f} ms**.",
            f"- Enriched ingest latency p95: **{aggregate['enriched_ingest_ms_p95']:.3f} ms**.",
            f"- Hook counter committed-append latency p95: **{aggregate['hook_counter_append_ms_p95']:.3f} ms**.",
            f"- SQLite storage overhead: **{aggregate['storage_overhead_bytes']} bytes**.",
            f"- Critical signal recall: **{aggregate['critical_signal_recall']:.1%}**.",
            f"- Exact source recovery: **{aggregate['exact_source_recovery_rate']:.1%}**.",
            f"- Path/line reference recall: **{aggregate['path_reference_recall']:.1%}**.",
            f"- Immediate verbatim line recall: **{aggregate['immediate_verbatim_line_recall']:.1%}**.",
            "",
            "## Gates",
            "",
            *[f"- {'PASS' if passed else 'FAIL'}: `{name}`" for name, passed in report["gates"].items()],
            "",
            "Verbatim line recall is diagnostic, not a pass gate: compact views intentionally omit "
            "repetitive success lines. Exact immutable source recovery is the losslessness gate.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], output_directory: str | Path) -> tuple[Path, Path]:
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "latest.json"
    markdown_path = directory / "latest.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path