"""Hook-path timing benchmark (task6A).

Measures what a host actually pays per hook delivery, per scenario, in two
plugin modes: the core alone and whatever plugins the environment has
installed. Zero behavior change — this module only drives existing surfaces.

Scenarios (task6.md 6A):
  capture_only        PostToolUse with a small, non-reducible tool output
  capture_with_reducer PostToolUse with a large generic output (views built)
  prompt_submit_resume UserPromptSubmit delivering the resume injection
  compact_path        PreCompact (consolidate + compact + snapshot)
  reconcile_idle      reconciler pass with no open tasks
  reconcile_pending   reconciler pass with an open task + matching evidence

Budgets are deliberately loose (order-of-magnitude tripwires, not
micro-benchmarks): they exist so a silent hot-path regression fails
``--assert-gates`` in CI, on any reasonable machine.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .hooks import process_hook
from .service import MemoryService

# Loose initial budgets, milliseconds, p95. Chosen ~3-5x above measured
# p95 on the development machine (2026-07-15) so they trip on structural
# regressions, not on machine variance. Revisit deliberately, not casually.
BUDGETS_MS = {
    "capture_only": 250.0,
    # The reducer tail is variance-prone (p50 ~52ms, p95 ~390ms measured
    # 2026-07-15): budget sized for the tail, not the median.
    "capture_with_reducer": 1000.0,
    "prompt_submit_resume": 1500.0,
    "compact_path": 2000.0,
    "reconcile_idle": 250.0,
    "reconcile_pending": 500.0,
}

_SMALL_OUTPUT = "ok: 3 files changed"
_LARGE_OUTPUT = "\n".join(
    f"log line {index}: benchmark payload with enough words to reduce"
    for index in range(300)
)


def _payload(event: str, session: str, **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": event, "session_id": session, **extra}


def _scenarios(
    service: MemoryService, session: str
) -> dict[str, Callable[[], Any]]:
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


def _seed_state(service: MemoryService, session: str) -> None:
    """A store that resembles a small live project: some history, one
    decision block, one open task (used by reconcile_pending)."""
    service.initialize_project()
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


def _measure(fn: Callable[[], Any], repetitions: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    samples.sort()
    return {
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(samples[max(0, int(len(samples) * 0.95) - 1)], 3),
        "max_ms": round(samples[-1], 3),
        "samples": len(samples),
    }


class _EmptyPlugins:
    def __init__(self) -> None:
        self.semantic: dict = {}
        self.knowledge_graph: dict = {}
        self.extractors: dict = {}
        self.kv_tiers: dict = {}
        self.rerankers: dict = {}
        self.errors: list[str] = []


def run_hook_timing(
    project_root: str | Path, *, repetitions: int = 30
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    runtime = root / "benchmarks" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    modes: dict[str, Any] = {}
    for mode, plugins in (("core_only", _EmptyPlugins()), ("installed_plugins", None)):
        run_id = uuid.uuid4().hex
        session = f"timing-{run_id[:8]}"
        kwargs = {"plugins": plugins} if plugins is not None else {}
        service = MemoryService(
            runtime / f"hook-timing-{run_id}.db", project_root=root, **kwargs
        )
        try:
            _seed_state(service, session)
            sections: dict[str, Any] = {}
            for name, fn in _scenarios(service, session).items():
                sections[name] = _measure(fn, repetitions)
            # reconcile with a pending candidate: plant evidence, measure the
            # detection-bearing pass (receipts make repeats idempotent — the
            # scan itself is what we time).
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
            sections["reconcile_pending"] = _measure(
                service.reconciler.reconcile, repetitions
            )
            plugin_names = sorted(service.plugins.semantic) + sorted(
                service.plugins.rerankers
            )
            modes[mode] = {"sections": sections, "plugins": plugin_names}
        finally:
            service.close()
    gates = {}
    for name, budget in BUDGETS_MS.items():
        worst = max(
            mode_data["sections"][name]["p95_ms"] for mode_data in modes.values()
        )
        gates[f"{name}_p95_under_{int(budget)}ms"] = worst <= budget
    return {
        "schema": "joiny-mnemonic-hook-timing-v1",
        "repetitions": repetitions,
        "budgets_ms": BUDGETS_MS,
        "modes": modes,
        "gates": gates,
        "passed": all(gates.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="joiny-mnemonic-hook-timing")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--output", default="benchmarks/results")
    parser.add_argument("--assert-gates", action="store_true")
    args = parser.parse_args(argv)
    report = run_hook_timing(args.project_root, repetitions=args.repetitions)
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
