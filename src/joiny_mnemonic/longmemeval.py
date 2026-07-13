"""LongMemEval harness (task5.md Part C).

Runs the LongMemEval-S benchmark against Joiny-Mnemonic ingestion and
retrieval. The core stays LLM-free: answer generation and judging are
delegated to one external runner command (JSON on stdin, JSON on stdout),
mirroring the evaluate-runner protocol. Judge prompts are reproduced
verbatim from "Hindsight is 20/20" (arXiv 2512.12818, Appendix A.4), which
follows the original LongMemEval judging setup; per-type binary accuracy is
the reported metric.

Runner contract (one command, two modes):
  stdin  {"mode": "answer", "question": ..., "question_date": ...,
          "context": ..., "question_id": ..., "question_type": ...}
  stdout {"output": "<answer text>"}

  stdin  {"mode": "judge", "prompt": "<filled judge prompt>",
          "question_id": ...}
  stdout {"output": "<reasoning ... \\boxed{yes|no}>"}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .prompt import conservative_token_estimate
from .service import MemoryService


_JUDGE_COMMON = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also "
    "answer yes. If the response only contains a subset of the information "
    "required by the answer, answer no."
)
_JUDGE_TAIL = (
    "Question: {question}\n"
    "Correct Answer: {answer}\n"
    "Model Response: {response}\n"
    "Is the model response correct?\n"
    "You may provide reasoning, but you MUST end your response with your final "
    "answer in the format: \\boxed{{yes}} or \\boxed{{no}}"
)

JUDGE_PROMPTS = {
    "default": _JUDGE_COMMON + "\n" + _JUDGE_TAIL,
    "temporal-reasoning": (
        _JUDGE_COMMON
        + " In addition, do not penalize off-by-one errors for the number of "
        "days. If the question asks for the number of days/weeks/months, etc., "
        "and the model makes off-by-one errors (e.g., predicting 19 days when "
        "the answer is 18), the model's response is still correct.\n"
        + _JUDGE_TAIL
    ),
    "knowledge-update": (
        "I will give you a question, a correct answer, and a response from a "
        "model. Please answer yes if the response contains the correct answer. "
        "Otherwise, answer no. If the response contains some previous "
        "information along with an updated answer, the response should be "
        "considered as correct as long as the updated answer is the required "
        "answer.\n" + _JUDGE_TAIL
    ),
    "preference": (
        "I will give you a question, a rubric for desired personalized "
        "response, and a response from a model. Please answer yes if the "
        "response satisfies the desired response. Otherwise, answer no. The "
        "model does not need to reflect all the points in the rubric. The "
        "response is correct as long as it recalls and utilizes the user's "
        "personal information correctly.\n"
        "Question: {question}\n"
        "Rubric: {answer}\n"
        "Model Response: {response}\n"
        "Is the model response correct?\n"
        "You may provide reasoning, but you MUST end your response with your "
        "final answer in the format: \\boxed{{yes}} or \\boxed{{no}}"
    ),
    "abstention": (
        "I will give you an unanswerable question, an explanation, and a "
        "response from a model. Please answer yes if the model correctly "
        "identifies the question as unanswerable. The model could say that the "
        "information is incomplete, or some other information is given but the "
        "asked information is not.\n"
        "Question: {question}\n"
        "Explanation: {answer}\n"
        "Model Response: {response}\n"
        "Does the model correctly identify the question as unanswerable?\n"
        "You may provide reasoning, but you MUST end your response with your "
        "final answer in the format: \\boxed{{yes}} or \\boxed{{no}}"
    ),
}

_BOXED = re.compile(r"\\boxed\{(yes|no)\}", re.IGNORECASE)


def judge_prompt_for(question_id: str, question_type: str) -> str:
    if str(question_id).endswith("_abs"):
        return JUDGE_PROMPTS["abstention"]
    if "preference" in question_type:
        return JUDGE_PROMPTS["preference"]
    if question_type == "temporal-reasoning":
        return JUDGE_PROMPTS["temporal-reasoning"]
    if question_type == "knowledge-update":
        return JUDGE_PROMPTS["knowledge-update"]
    return JUDGE_PROMPTS["default"]


def parse_boxed(text: str) -> bool | None:
    matches = _BOXED.findall(text or "")
    if not matches:
        return None
    return matches[-1].casefold() == "yes"


@dataclass(frozen=True, slots=True)
class LMEQuestion:
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str
    sessions: tuple[dict[str, Any], ...]


def load_dataset(path: str | Path) -> list[LMEQuestion]:
    """LongMemEval-S: one haystack of dated sessions per question."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items: list[LMEQuestion] = []
    for entry in raw:
        ids = entry.get("haystack_session_ids") or []
        dates = entry.get("haystack_dates") or []
        sessions = entry.get("haystack_sessions") or []
        packed = tuple(
            {
                "session_id": ids[index] if index < len(ids) else f"session-{index}",
                "date": dates[index] if index < len(dates) else "",
                "turns": sessions[index],
            }
            for index in range(len(sessions))
        )
        items.append(
            LMEQuestion(
                question_id=str(entry["question_id"]),
                question_type=str(entry.get("question_type", "unknown")),
                question=str(entry["question"]),
                answer=str(entry.get("answer", "")),
                question_date=str(entry.get("question_date", "")),
                sessions=packed,
            )
        )
    return items


class SubprocessLLMRunner:
    """One external command answers and judges; stdin/stdout JSON."""

    def __init__(self, command: Sequence[str], *, timeout_seconds: float = 300) -> None:
        if not command:
            raise ValueError("runner command must be non-empty")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    _ATTEMPTS = 3
    _BACKOFF_SECONDS = (5.0, 20.0)

    def call(self, request: dict[str, Any]) -> str:
        # Field finding (2026-07-14): a Windows child Python writes piped
        # stderr in the ANSI code page, and a strict-utf8 reader thread
        # crash then masked the real error — decode tolerantly and pin the
        # child to UTF-8. Transient runner failures retry with backoff; one
        # flake out of a thousand calls must not kill a two-hour run.
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        last_error: Exception | None = None
        for attempt in range(self._ATTEMPTS):
            if attempt:
                time.sleep(self._BACKOFF_SECONDS[min(attempt - 1, 1)])
            try:
                completed = subprocess.run(
                    self.command,
                    input=json.dumps(request, ensure_ascii=False),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                last_error = exc
                continue
            if completed.returncode != 0:
                last_error = RuntimeError(
                    f"runner failed ({completed.returncode}): "
                    f"{completed.stderr[-2000:]}"
                )
                continue
            try:
                value = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                last_error = RuntimeError(
                    f"runner emitted invalid JSON: {exc}; "
                    f"stdout tail: {completed.stdout[-500:]}"
                )
                continue
            if not isinstance(value, dict) or "output" not in value:
                raise ValueError("runner must return a JSON object with 'output'")
            return str(value["output"])
        raise RuntimeError(
            f"runner failed after {self._ATTEMPTS} attempts: {last_error}"
        )


@dataclass
class LMEHarness:
    runner: SubprocessLLMRunner
    context_budget_tokens: int = 4096
    retrieval_limit: int = 24

    results: list[dict[str, Any]] = field(default_factory=list)

    def ingest(self, service: MemoryService, item: LMEQuestion) -> int:
        """Sessions become dated message events; the date rides in the text
        (Hindsight's measured cheap win) because admission time is now."""
        count = 0
        for session in item.sessions:
            date = session["date"]
            for turn in session["turns"]:
                content = str(turn.get("content", "")).strip()
                if not content:
                    continue
                prefix = f"[Date: {date}] " if date else ""
                service.store.append_event(
                    kind="message",
                    role=str(turn.get("role", "user")),
                    content=prefix + content,
                    payload={
                        "session_id": session["session_id"],
                        "session_date": date,
                    },
                )
                count += 1
        return count

    def build_context(self, service: MemoryService, item: LMEQuestion) -> tuple[str, list[str]]:
        hits = service.search(
            query=item.question,
            limit=self.retrieval_limit,
            include_events=True,
            record_telemetry=False,
        )
        parts: list[str] = []
        included: list[str] = []
        used = 0
        for hit in hits:
            block = hit.content.strip()
            cost = conservative_token_estimate(block)
            if used + cost > self.context_budget_tokens:
                break
            parts.append(block)
            included.append(hit.id)
            used += cost
        return "\n\n".join(parts), included

    def run_question(self, item: LMEQuestion) -> dict[str, Any]:
        started = time.perf_counter()
        with MemoryService(":memory:", project_root=Path.cwd()) as service:
            ingested = self.ingest(service, item)
            context, included = self.build_context(service, item)
        answer = self.runner.call(
            {
                "mode": "answer",
                "question": item.question,
                "question_date": item.question_date,
                "context": context,
                "question_id": item.question_id,
                "question_type": item.question_type,
            }
        )
        prompt = judge_prompt_for(item.question_id, item.question_type).format(
            question=item.question, answer=item.answer, response=answer
        )
        verdict_text = self.runner.call(
            {"mode": "judge", "prompt": prompt, "question_id": item.question_id}
        )
        verdict = parse_boxed(verdict_text)
        record = {
            "question_id": item.question_id,
            "question_type": item.question_type,
            "correct": bool(verdict),
            "judge_parseable": verdict is not None,
            "answer": answer,
            "judge_output": verdict_text,
            "retrieved_ids": included,
            "ingested_events": ingested,
            "latency_seconds": round(time.perf_counter() - started, 3),
        }
        self.results.append(record)
        return record

    def report(self) -> dict[str, Any]:
        by_type: dict[str, dict[str, int]] = {}
        for record in self.results:
            bucket = by_type.setdefault(
                record["question_type"], {"total": 0, "correct": 0}
            )
            bucket["total"] += 1
            bucket["correct"] += int(record["correct"])
        overall_total = sum(item["total"] for item in by_type.values())
        overall_correct = sum(item["correct"] for item in by_type.values())
        return {
            "benchmark": "LongMemEval-S",
            "harness": "joiny-mnemonic-longmemeval",
            "judge_prompts": "arXiv 2512.12818 Appendix A.4 (verbatim)",
            "config": {
                "context_budget_tokens": self.context_budget_tokens,
                "retrieval_limit": self.retrieval_limit,
                "runner_command": list(self.runner.command),
            },
            "per_type": {
                name: {
                    **counts,
                    "accuracy": round(counts["correct"] / counts["total"], 4),
                }
                for name, counts in sorted(by_type.items())
            },
            "overall": {
                "total": overall_total,
                "correct": overall_correct,
                "accuracy": round(overall_correct / overall_total, 4)
                if overall_total else 0.0,
            },
            "unparseable_judgments": sum(
                1 for record in self.results if not record["judge_parseable"]
            ),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="joiny-mnemonic-longmemeval")
    parser.add_argument("dataset", help="LongMemEval-S JSON file")
    parser.add_argument(
        "--runner-command", required=True,
        help='JSON array, e.g. \'["python","my_llm_runner.py"]\'',
    )
    parser.add_argument("--budget", type=int, default=4096)
    parser.add_argument("--retrieval-limit", type=int, default=24)
    parser.add_argument("--limit-questions", type=int, default=0)
    parser.add_argument(
        "--offset", type=int, default=0,
        help="skip the first N questions and append to existing results "
        "(resume support for long runs)",
    )
    parser.add_argument("--output-dir", default="benchmarks/results")
    args = parser.parse_args(argv)

    command = json.loads(args.runner_command)
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
        raise SystemExit("--runner-command must be a JSON array of strings")
    items = load_dataset(args.dataset)
    if args.offset:
        items = items[args.offset:]
    if args.limit_questions:
        items = items[: args.limit_questions]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "longmemeval-latest.jsonl"
    # Each record is flushed as soon as it is judged, so a crash mid-run
    # loses nothing; --offset appends to the same file to resume.
    harness = LMEHarness(
        SubprocessLLMRunner(command),
        context_budget_tokens=args.budget,
        retrieval_limit=args.retrieval_limit,
    )
    mode = "a" if args.offset else "w"
    with jsonl_path.open(mode, encoding="utf-8") as stream:
        for index, item in enumerate(items, 1):
            record = harness.run_question(item)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            print(
                f"[{args.offset + index}/{args.offset + len(items)}] "
                f"{item.question_id} {'OK' if record['correct'] else 'MISS'}",
                flush=True,
            )

    # The report always covers every row in the JSONL, including rows from
    # earlier resumed segments.
    all_rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    harness.results = all_rows
    report = harness.report()
    dataset_hash = hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()[:16]
    report["dataset_sha256_16"] = dataset_hash
    (output_dir / "longmemeval-latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["overall"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
