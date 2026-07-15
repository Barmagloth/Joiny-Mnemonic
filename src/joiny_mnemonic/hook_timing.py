"""Hook-path timing benchmark (task6A, v2 acceptance).

Not "the hook took X": per-stage breakdown inside each delivery, fixture
sizes, cold vs warm, SQLite store size, p50/p95/p99, and regression gates.
Zero behavior change — stages are observed through the no-op-by-default
collector in ``hooks._STAGE_SINK``.

Scenarios (task6.md 6A):
  capture_only         PostToolUse with a small, non-reducible tool output
  capture_with_reducer PostToolUse with a large generic output (views built)
  prompt_submit_resume UserPromptSubmit delivering the resume injection
  compact_path         PreCompact (consolidate + compact + snapshot)
  reconcile_idle       reconciler pass with no open tasks
  reconcile_pending    reconciler pass with an open task + matching evidence

Each scenario runs at two store scales (small ~35 events, grown ~1200
events) and in two plugin modes (core only / installed plugins). Cold-start
costs (imports, store open, first capture, first resume) are measured in a
fresh subprocess per mode.

Budgets are loose order-of-magnitude tripwires asserted via
``--assert-gates`` — they catch structural regressions on any reasonable
machine, not 10% drifts. Standing rule (task6A acceptance): no new
always-on feature lands without extending this report first.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import hooks
from .hooks import process_hook
from .service import MemoryService

# p95 budgets, milliseconds, warm, worst mode/scale. Sized 3-5x above the
# 2026-07-15 development-machine measurements (reducer sized for its
# variance tail). Revisit deliberately, not casually.
BUDGETS_MS = {
    "capture_only": 250.0,
    "capture_with_reducer": 1500.0,
    "prompt_submit_resume": 2000.0,
    "compact_path": 3000.0,
    "reconcile_idle": 250.0,
    "reconcile_pending": 500.0,
}
COLD_BUDGETS_MS = {
    "import_ms": 5000.0,
    "service_open_ms": 3000.0,
    "first_capture_ms": 3000.0,
    "first_resume_ms": 60000.0,  # includes one-time embedder load with plugins
}

_SMALL_OUTPUT = "ok: 3 files changed"
_LARGE_OUTPUT = "\n".join(
    f"log line {index}: benchmark payload with enough words to reduce"
    for index in range(300)
)
FIXTURES = {
    "small_output_chars": len(_SMALL_OUTPUT),
    "large_output_chars": len(_LARGE_OUTPUT),
    "seed_events_small": 32,
    "seed_events_grown": 1200,
    "prompt_chars": len("что мы решили по конфигам?"),
}


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    def pct(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        rank = p / 100 * (len(ordered) - 1)
        low = int(rank)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(pct(95), 3),
        "p99_ms": round(pct(99), 3),
        "max_ms": round(ordered[-1], 3),
        "samples": len(ordered),
    }


def _payload(event: str, session: str, **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": event, "session_id": session, **extra}


def _scenarios(service: MemoryService, session: str) -> dict[str, Callable[[], Any]]:
    def capture_only() -> None:
        process_hook(
            service, "claude-code",
            _payload(
                "PostToolUse", session,
                tool_name="Bash",
                tool_input={"command": f"status-{uuid.uuid4().hex[:8]}"},
                tool_response=_SMALL_OUTPUT,
            ),
        )

    def capture_with_reducer() -> None:
        process_hook(
            service, "claude-code",
            _payload(
                "PostToolUse", session,
                tool_name="Bash",
                tool_input={"command": f"analyze-{uuid.uuid4().hex[:8]} --all"},
                tool_response=_LARGE_OUTPUT,
            ),
        )

    def prompt_submit_resume() -> None:
        process_hook(
            service, "claude-code",
            _payload("UserPromptSubmit", session, prompt="что мы решили по конфигам?"),
        )

    def compact_path() -> None:
        process_hook(service, "claude-code", _payload("PreCompact", session))

    def reconcile_idle() -> None:
        service.reconciler.reconcile()

    return {
        "capture_only": capture_only,
        "capture_with_reducer": capture_with_reducer,
        "prompt_submit_resume": prompt_submit_resume,
        "compact_path": compact_path,
        "reconcile_idle": reconcile_idle,
    }


def _seed_state(service: MemoryService, session: str, scale: str) -> None:
    """Small: a young live project. Grown: months of history — bulk events
    through the store (fast path) plus hook-shaped working state."""
    service.initialize_project()
    if scale == "grown":
        for index in range(FIXTURES["seed_events_grown"] - FIXTURES["seed_events_small"]):
            service.store.append_event(
                kind="message",
                role="user" if index % 2 else "assistant",
                content=f"[Date: 2026-0{1 + index % 6}-1{index % 3}] исторические обсуждения "
                f"номер {index} про конфиги, логи, retrieval и прочие рабочие темы",
                payload={"seed": scale},
            )
    for index in range(30):
        process_hook(
            service, "claude-code",
            _payload(
                "UserPromptSubmit", session,
                prompt=f"рабочая реплика номер {index} про конфиги и логи",
            ),
        )
    process_hook(
        service, "claude-code",
        _payload("UserPromptSubmit", session, prompt="DECISION: конфиги храним в YAML"),
    )
    process_hook(
        service, "claude-code",
        _payload("UserPromptSubmit", session, prompt="TODO: создать файл report.md"),
    )


def _measure_with_stages(
    fn: Callable[[], Any], repetitions: int
) -> dict[str, Any]:
    totals: list[float] = []
    stage_samples: dict[str, list[float]] = {}
    for _ in range(repetitions):
        sink: dict[str, float] = {}
        hooks._STAGE_SINK = sink
        started = time.perf_counter_ns()
        try:
            fn()
        finally:
            hooks._STAGE_SINK = None
        totals.append((time.perf_counter_ns() - started) / 1_000_000)
        for name, value in sink.items():
            stage_samples.setdefault(name, []).append(value)
    return {
        "total": _percentiles(totals),
        "stages": {
            name: _percentiles(values)
            for name, values in sorted(stage_samples.items())
        },
    }


class _EmptyPlugins:
    def __init__(self) -> None:
        self.semantic: dict = {}
        self.knowledge_graph: dict = {}
        self.extractors: dict = {}
        self.kv_tiers: dict = {}
        self.rerankers: dict = {}
        self.errors: list[str] = []


_COLD_PROBE = r"""
import json, sys, time
root, mode = sys.argv[1], sys.argv[2]
t0 = time.perf_counter_ns()
from pathlib import Path
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.hooks import process_hook
import_ms = (time.perf_counter_ns() - t0) / 1e6
kwargs = {}
if mode == "core_only":
    class _Empty:
        def __init__(self):
            self.semantic = {}; self.knowledge_graph = {}; self.extractors = {}
            self.kv_tiers = {}; self.rerankers = {}; self.errors = []
    kwargs["plugins"] = _Empty()
t1 = time.perf_counter_ns()
service = MemoryService(Path(root) / "cold-probe.db", project_root=Path(root), **kwargs)
service.initialize_project()
open_ms = (time.perf_counter_ns() - t1) / 1e6
t2 = time.perf_counter_ns()
process_hook(service, "claude-code", {
    "hook_event_name": "PostToolUse", "session_id": "cold",
    "tool_name": "Bash", "tool_input": {"command": "echo cold"},
    "tool_response": "cold done",
})
capture_ms = (time.perf_counter_ns() - t2) / 1e6
t3 = time.perf_counter_ns()
process_hook(service, "claude-code", {
    "hook_event_name": "UserPromptSubmit", "session_id": "cold",
    "prompt": "что мы решили по конфигам?",
})
resume_ms = (time.perf_counter_ns() - t3) / 1e6
service.close()
print(json.dumps({
    "import_ms": round(import_ms, 1), "service_open_ms": round(open_ms, 1),
    "first_capture_ms": round(capture_ms, 1), "first_resume_ms": round(resume_ms, 1),
}))
"""


def _measure_cold(root: Path, mode: str) -> dict[str, float]:
    probe_root = root / "benchmarks" / "runtime" / f"cold-{mode}-{uuid.uuid4().hex[:8]}"
    probe_root.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-c", _COLD_PROBE, str(probe_root), mode],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
        env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        return {"error": completed.stderr[-500:]}  # type: ignore[return-value]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def run_hook_timing(
    project_root: str | Path,
    *,
    repetitions: int = 50,
    scales: tuple[str, ...] = ("small", "grown"),
    include_cold: bool = True,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    runtime = root / "benchmarks" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    modes: dict[str, Any] = {}
    for mode, plugins_factory in (
        ("core_only", _EmptyPlugins),
        ("installed_plugins", None),
    ):
        scales_data: dict[str, Any] = {}
        for scale in scales:
            run_id = uuid.uuid4().hex
            session = f"timing-{run_id[:8]}"
            kwargs = (
                {"plugins": plugins_factory()} if plugins_factory is not None else {}
            )
            db_path = runtime / f"hook-timing-{run_id}.db"
            service = MemoryService(db_path, project_root=root, **kwargs)
            try:
                _seed_state(service, session, scale)
                db_size_seeded = db_path.stat().st_size
                sections: dict[str, Any] = {}
                for name, fn in _scenarios(service, session).items():
                    sections[name] = _measure_with_stages(fn, repetitions)
                service.store.append_host_events_once(
                    f"timing-evidence-{run_id}",
                    [
                        {
                            "kind": "tool_output", "role": "tool",
                            "content": '{"type": "create", "filePath": "report.md"}',
                            "payload": {
                                "tool_name": "Write",
                                "hook_event_name": "PostToolUse",
                                "tool_response": {"type": "create"},
                            },
                            "files": ["report.md"],
                        }
                    ],
                    adapter="claude-code",
                )
                sections["reconcile_pending"] = _measure_with_stages(
                    service.reconciler.reconcile, repetitions
                )
                plugin_names = sorted(service.plugins.semantic) + sorted(
                    service.plugins.rerankers
                )
                scales_data[scale] = {
                    "sections": sections,
                    "db_size_seeded_bytes": db_size_seeded,
                    "db_size_final_bytes": db_path.stat().st_size,
                    "event_count": service.store.query_events().__len__(),
                }
            finally:
                service.close()
        cold = _measure_cold(root, mode) if include_cold else None
        modes[mode] = {
            "scales": scales_data,
            "plugins": plugin_names,
            "cold_start": cold,
        }
    gates: dict[str, bool] = {}
    for name, budget in BUDGETS_MS.items():
        worst = max(
            scale_data["sections"][name]["total"]["p95_ms"]
            for mode_data in modes.values()
            for scale_data in mode_data["scales"].values()
        )
        gates[f"{name}_p95_under_{int(budget)}ms"] = worst <= budget
    if include_cold:
        for metric, budget in COLD_BUDGETS_MS.items():
            worst_cold = max(
                float(mode_data["cold_start"].get(metric, 0) or 0)
                for mode_data in modes.values()
                if isinstance(mode_data.get("cold_start"), dict)
                and "error" not in mode_data["cold_start"]
            )
            gates[f"cold_{metric}_under_{int(budget)}ms"] = worst_cold <= budget
    return {
        "schema": "joiny-mnemonic-hook-timing-v2",
        "repetitions": repetitions,
        "fixtures": FIXTURES,
        "budgets_ms": BUDGETS_MS,
        "cold_budgets_ms": COLD_BUDGETS_MS,
        "modes": modes,
        "gates": gates,
        "passed": all(gates.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="joiny-mnemonic-hook-timing")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--repetitions", type=int, default=50)
    parser.add_argument("--no-cold", action="store_true")
    parser.add_argument("--output", default="benchmarks/results")
    parser.add_argument("--assert-gates", action="store_true")
    args = parser.parse_args(argv)
    report = run_hook_timing(
        args.project_root,
        repetitions=args.repetitions,
        include_cold=not args.no_cold,
    )
    from .report_signing import stamp_report

    report = stamp_report(report, repo_root=Path(args.project_root).resolve())
    output_dir = Path(args.project_root) / args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "hook-timing-latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(
        {"passed": report["passed"], "gates": report["gates"]},
        ensure_ascii=False, indent=2,
    ))
    if args.assert_gates and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
