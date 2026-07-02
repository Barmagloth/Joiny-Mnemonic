from __future__ import annotations

import time
import json
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence

from .prompt import conservative_token_estimate
from .service import MemoryService


@dataclass(frozen=True, slots=True)
class EvaluationTask:
    id: str
    query: str
    required_evidence: tuple[str, ...] = ()
    branch_id: str = "main"
    task_input: str = ""
    expected_output: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PolicyOutput:
    text: str
    token_cost: int


class MemoryPolicy(Protocol):
    name: str

    def render(self, service: MemoryService, task: EvaluationTask) -> PolicyOutput: ...


@dataclass(frozen=True, slots=True)
class TaskRun:
    success: bool
    output: str
    score: float
    metadata: dict[str, Any] | None = None


class TaskRunner(Protocol):
    name: str

    def run(self, task: EvaluationTask, context: PolicyOutput) -> TaskRun: ...


class SubprocessTaskRunner:
    """JSON stdin/stdout bridge to an actual LLM or executable task harness."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 300,
        name: str = "subprocess-task-runner",
    ) -> None:
        if not command:
            raise ValueError("runner command must be non-empty")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.name = name

    def run(self, task: EvaluationTask, context: PolicyOutput) -> TaskRun:
        payload = {
            "task": asdict(task),
            "context": context.text,
            "context_tokens": context.token_cost,
        }
        completed = subprocess.run(
            self.command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            return TaskRun(
                success=False,
                output=completed.stdout,
                score=0.0,
                metadata={
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-4000:],
                },
            )
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise ValueError("task runner output must be a JSON object")
        success = bool(value.get("success", False))
        score = float(value.get("score", 1.0 if success else 0.0))
        if not 0.0 <= score <= 1.0:
            raise ValueError("task runner score must be between 0 and 1")
        return TaskRun(
            success=success,
            output=str(value.get("output", "")),
            score=score,
            metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else None,
        )


class FullHistoryPolicy:
    name = "full-history"

    def render(self, service: MemoryService, task: EvaluationTask) -> PolicyOutput:
        blocks = service.store.get_active_blocks(branch_id=task.branch_id)
        events = service.store.query_events(branch_id=task.branch_id)
        text = "\n".join(
            [*(block.content for block in blocks.values()), *(event.content for event in events)]
        )
        return PolicyOutput(text=text, token_cost=conservative_token_estimate(text))


class ResumePolicy:
    def __init__(self, token_budget: int = 1500) -> None:
        self.token_budget = min(token_budget, 1500)
        self.name = f"resume-{self.token_budget}"

    def render(self, service: MemoryService, task: EvaluationTask) -> PolicyOutput:
        packet = service.resume(
            branch_id=task.branch_id,
            token_budget=self.token_budget,
            query=task.query,
        )
        return PolicyOutput(text=packet.text, token_cost=packet.estimated_tokens)


def _quality(output: str, evidence: Sequence[str]) -> float:
    if not evidence:
        return 1.0
    folded = output.casefold()
    return sum(item.casefold() in folded for item in evidence) / len(evidence)


def evaluate_policies(
    service: MemoryService,
    tasks: Sequence[EvaluationTask],
    policies: Sequence[MemoryPolicy] | None = None,
) -> dict[str, object]:
    """Compare policies at task level on quality, token cost, latency and storage."""
    policies = policies or (FullHistoryPolicy(), ResumePolicy())
    rows: list[dict[str, object]] = []
    by_task: dict[str, dict[str, float]] = {}
    for task in tasks:
        by_task[task.id] = {}
        for policy in policies:
            started = time.perf_counter()
            output = policy.render(service, task)
            latency_ms = (time.perf_counter() - started) * 1000
            quality = _quality(output.text, task.required_evidence)
            by_task[task.id][policy.name] = quality
            rows.append(
                {
                    "task_id": task.id,
                    "policy": policy.name,
                    "quality": quality,
                    "token_cost": output.token_cost,
                    "latency_ms": latency_ms,
                    "storage_bytes": service.store.database_size(),
                }
            )
    baseline = FullHistoryPolicy.name
    for row in rows:
        baseline_quality = by_task[str(row["task_id"])].get(baseline, 1.0)
        row["quality_vs_full_history"] = (
            float(row["quality"]) / baseline_quality if baseline_quality else 1.0
        )
    aggregates: dict[str, dict[str, float]] = {}
    for policy in {str(row["policy"]) for row in rows}:
        selected = [row for row in rows if row["policy"] == policy]
        count = max(len(selected), 1)
        aggregates[policy] = {
            "quality": sum(float(row["quality"]) for row in selected) / count,
            "quality_vs_full_history": sum(
                float(row["quality_vs_full_history"]) for row in selected
            ) / count,
            "token_cost": sum(float(row["token_cost"]) for row in selected) / count,
            "latency_ms": sum(float(row["latency_ms"]) for row in selected) / count,
            "storage_bytes": max((float(row["storage_bytes"]) for row in selected), default=0),
        }
    return {
        "evaluation_mode": "evidence-presence-diagnostic",
        "task_level": False,
        "tasks": [asdict(task) for task in tasks],
        "results": rows,
        "aggregates": aggregates,
    }


def evaluate_with_runner(
    service: MemoryService,
    tasks: Sequence[EvaluationTask],
    runner: TaskRunner,
    policies: Sequence[MemoryPolicy] | None = None,
) -> dict[str, object]:
    """Run real tasks under each memory policy and compare outcome quality/cost."""
    policies = policies or (FullHistoryPolicy(), ResumePolicy())
    rows: list[dict[str, object]] = []
    scores: dict[str, dict[str, float]] = {}
    for task in tasks:
        scores[task.id] = {}
        for policy in policies:
            render_started = time.perf_counter()
            context = policy.render(service, task)
            render_ms = (time.perf_counter() - render_started) * 1000
            run_started = time.perf_counter()
            outcome = runner.run(task, context)
            run_ms = (time.perf_counter() - run_started) * 1000
            scores[task.id][policy.name] = outcome.score
            rows.append(
                {
                    "task_id": task.id,
                    "policy": policy.name,
                    "success": outcome.success,
                    "score": outcome.score,
                    "output": outcome.output,
                    "runner_metadata": outcome.metadata or {},
                    "token_cost": context.token_cost,
                    "render_latency_ms": render_ms,
                    "task_latency_ms": run_ms,
                    "storage_bytes": service.store.database_size(),
                }
            )
    for row in rows:
        baseline = scores[str(row["task_id"])].get(FullHistoryPolicy.name, 0.0)
        row["score_vs_full_history"] = float(row["score"]) / baseline if baseline else (
            1.0 if float(row["score"]) == 0.0 else 0.0
        )
    aggregates: dict[str, dict[str, float]] = {}
    for policy in {str(row["policy"]) for row in rows}:
        selected = [row for row in rows if row["policy"] == policy]
        count = max(1, len(selected))
        aggregates[policy] = {
            "success_rate": sum(bool(row["success"]) for row in selected) / count,
            "score": sum(float(row["score"]) for row in selected) / count,
            "score_vs_full_history": sum(float(row["score_vs_full_history"]) for row in selected) / count,
            "token_cost": sum(float(row["token_cost"]) for row in selected) / count,
            "render_latency_ms": sum(float(row["render_latency_ms"]) for row in selected) / count,
            "task_latency_ms": sum(float(row["task_latency_ms"]) for row in selected) / count,
        }
    return {
        "evaluation_mode": "external-task-runner",
        "task_level": True,
        "runner": runner.name,
        "tasks": [asdict(task) for task in tasks],
        "results": rows,
        "aggregates": aggregates,
    }


def assert_task_quality(report: dict[str, object], minimum: float = 0.95) -> None:
    if report.get("task_level") is not True:
        raise AssertionError("quality gates require a task-level runner report")
    aggregates = report.get("aggregates")
    if not isinstance(aggregates, dict):
        raise AssertionError("evaluation report has no aggregates")
    resume_rows = [value for key, value in aggregates.items() if str(key).startswith("resume-")]
    if not resume_rows:
        raise AssertionError("evaluation report contains no resume policy")
    for row in resume_rows:
        ratio = float(row["score_vs_full_history"])
        if ratio < minimum:
            raise AssertionError(
                f"resume task score {ratio:.3f} is below full-history ratio {minimum:.3f}"
            )


def assert_resume_quality(report: dict[str, object], minimum: float = 0.95) -> None:
    aggregates = report["aggregates"]
    assert isinstance(aggregates, dict)
    resume_rows = [value for key, value in aggregates.items() if str(key).startswith("resume-")]
    if not resume_rows:
        raise AssertionError("evaluation report contains no resume policy")
    for row in resume_rows:
        ratio = float(row["quality_vs_full_history"])
        if ratio < minimum:
            raise AssertionError(
                f"resume quality {ratio:.3f} is below required full-history ratio {minimum:.3f}"
            )
