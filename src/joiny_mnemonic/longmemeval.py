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
    answer_session_ids: tuple[str, ...] = ()


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
                answer_session_ids=tuple(
                    str(sid) for sid in entry.get("answer_session_ids") or ()
                ),
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
    packing: str = "rank"  # "rank" (greedy fused order) | "breadth" (see below)
    rerank: bool = False  # cross-encoder rerank of the candidate pool
    ingest_mode: str = "raw"  # "raw" turns | "distill" facts alongside turns
    distill_cache_dir: Path | None = None
    _cross_encoder: Any = field(default=None, repr=False)
    active_semantic_plugins: list[str] = field(default_factory=list)
    active_reranker_plugins: list[str] = field(default_factory=list)

    def _distill_session(self, session: dict[str, Any]) -> list[str]:
        """LLM fact extraction for one session, disk-cached by content hash
        (LongMemEval haystacks share sessions across questions). Fail-safe:
        unparseable output means no facts, never a crashed run."""
        transcript = "\n".join(
            f"[{turn.get('role', 'user')}] {turn.get('content', '')}"
            for turn in session["turns"]
        )
        key = hashlib.sha256(
            (session["session_id"] + transcript).encode("utf-8")
        ).hexdigest()[:24]
        cache_file = (
            self.distill_cache_dir / f"{key}.json"
            if self.distill_cache_dir else None
        )
        if cache_file is not None and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))
        output = self.runner.call(
            {
                "mode": "distill",
                "session_date": session["date"],
                "transcript": transcript[:60000],
                "session_id": session["session_id"],
            }
        )
        facts: list[str] = []
        try:
            start = output.index("[")
            end = output.rindex("]") + 1
            parsed = json.loads(output[start:end])
            facts = [str(item) for item in parsed if str(item).strip()]
        except (ValueError, json.JSONDecodeError):
            facts = []
        if cache_file is not None:
            self.distill_cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(facts, ensure_ascii=False), encoding="utf-8"
            )
        return facts

    def _rerank_hits(self, question: str, hits: list[Any]) -> list[Any]:
        """Cross-encoder rerank (calibration research 2026-07-14: +3-4pp in
        the Emergence stack; model = ms-marco-MiniLM-L-6-v2, the same local
        ~80MB cross-encoder Hindsight ships). Loaded lazily, cached across
        questions."""
        if self._cross_encoder is None:
            from sentence_transformers import CrossEncoder

            self._cross_encoder = CrossEncoder(
                "cross-encoder/ms-marco-MiniLM-L-6-v2"
            )
        scores = self._cross_encoder.predict(
            [(question, hit.content[:2000]) for hit in hits]
        )
        order = sorted(range(len(hits)), key=lambda i: scores[i], reverse=True)
        return [hits[i] for i in order]

    results: list[dict[str, Any]] = field(default_factory=list)

    def ingest(self, service: MemoryService, item: LMEQuestion) -> int:
        """Sessions become dated message events; the date rides in the text
        (Hindsight's measured cheap win) because admission time is now.

        In "distill" mode LLM-extracted facts are ADDED alongside the
        verbatim turns through the product derive path (provenance to the
        session's events) — the A/B tests our Phase B shape, facts next to
        verbatim, never a lossy replacement."""
        count = 0
        for session in item.sessions:
            date = session["date"]
            event_ids: list[str] = []
            for turn in session["turns"]:
                content = str(turn.get("content", "")).strip()
                if not content:
                    continue
                prefix = f"[Date: {date}] " if date else ""
                event = service.store.append_event(
                    kind="message",
                    role=str(turn.get("role", "user")),
                    content=prefix + content,
                    payload={
                        "session_id": session["session_id"],
                        "session_date": date,
                    },
                )
                event_ids.append(event.id)
                count += 1
            if self.ingest_mode == "distill" and event_ids:
                for fact in self._distill_session(session):
                    service.store.derive_memory(
                        memory_type="fact",
                        content=fact,
                        source_event_ids=tuple(event_ids),
                        valid_from=date[:10] if date else None,
                        metadata={"session_id": session["session_id"]},
                    )
        return count

    def build_context(
        self, service: MemoryService, item: LMEQuestion
    ) -> tuple[str, list[str], dict[str, Any]]:
        # question_date anchors relative expressions ("last Sunday") in the
        # question's own clock — without it the temporal arm parses windows
        # against the real now, years away from the haystack.
        anchor = f"{item.question_date}T12:00:00+00:00" if item.question_date else None
        hits = service.search(
            query=item.question,
            limit=self.retrieval_limit,
            include_events=True,
            record_telemetry=False,
            query_timestamp=anchor,
        )
        if self.rerank and hits:
            hits = self._rerank_hits(item.question, list(hits))
        # Session-diversity packing (probe finding 2026-07-14: aggregation
        # questions miss because RRF fills the packet with fragments of the
        # lexically strongest sessions, crowding out weaker gold sessions —
        # coverage, not synthesis, is the binding constraint). Two passes:
        # rank order with a per-session cap first, then backfill.
        def _hit_session(hit: Any) -> tuple[str, str]:
            try:
                event = service.store.get_event(hit.id)
                return (
                    str(event.payload.get("session_id") or ""),
                    str(event.payload.get("session_date") or ""),
                )
            except KeyError:
                pass
            try:  # distilled facts are memory records with session metadata
                record = service.store.get_memory(hit.id)
                return (
                    str(record.metadata.get("session_id") or ""),
                    str(record.valid_from or ""),
                )
            except KeyError:
                return ("", "")

        annotated = [(hit, *_hit_session(hit)) for hit in hits]
        chosen: list[tuple[Any, str, str]] = []
        used = 0
        if self.packing == "breadth":
            # Breadth-first (ceiling measurement 2026-07-14: the pool holds
            # 100% of gold sessions at limit 128, but rank-order packing
            # drowns the deep-ranked ones below the budget line; a hard
            # per-session cap traded depth away at a loss). Phase 1 packs
            # the first fragment of every unseen session in rank order —
            # cheap full-width coverage; phase 2 backfills depth by rank.
            seen_sessions: set[str] = set()
            picked_ids: set[int] = set()
            for index, entry in enumerate(annotated):
                hit, session_id, _ = entry
                if session_id and session_id in seen_sessions:
                    continue
                cost = conservative_token_estimate(hit.content.strip())
                if used + cost > self.context_budget_tokens:
                    continue
                chosen.append(entry)
                picked_ids.add(index)
                seen_sessions.add(session_id)
                used += cost
            for index, entry in enumerate(annotated):
                if index in picked_ids:
                    continue
                cost = conservative_token_estimate(entry[0].content.strip())
                if used + cost > self.context_budget_tokens:
                    continue
                chosen.append(entry)
                used += cost
        else:  # "rank": plain greedy in fused order
            for entry in annotated:
                cost = conservative_token_estimate(entry[0].content.strip())
                if used + cost > self.context_budget_tokens:
                    break
                chosen.append(entry)
                used += cost
        # Render grouped by session, sessions in date order — aggregation
        # becomes a walk over a structured list instead of needle-hunting.
        groups: dict[tuple[str, str], list[str]] = {}
        for hit, session_id, date in chosen:
            groups.setdefault((date, session_id), []).append(hit.content.strip())
        parts = []
        for (date, _), blocks in sorted(groups.items()):
            header = f"## Session {date}" if date else "## Session (undated)"
            parts.append(header + "\n" + "\n".join(blocks))
        included = [hit.id for hit, _, _ in chosen]
        retrieved_sessions = {sid for _, sid, _ in chosen if sid}
        haystack_tokens = sum(
            conservative_token_estimate(str(turn.get("content", "")))
            for session in item.sessions
            for turn in session["turns"]
        )
        gold = set(item.answer_session_ids)
        metrics = {
            "context_tokens": used,
            "haystack_tokens": haystack_tokens,
            "retrieved_sessions": sorted(retrieved_sessions),
            "gold_sessions": sorted(gold),
            "retrieval_hit": bool(gold & retrieved_sessions) if gold else None,
            "gold_coverage": round(
                len(gold & retrieved_sessions) / len(gold), 4
            ) if gold else None,
        }
        return "\n\n".join(parts), included, metrics

    def run_question(self, item: LMEQuestion) -> dict[str, Any]:
        started = time.perf_counter()
        with MemoryService(":memory:", project_root=Path.cwd()) as service:
            self.active_semantic_plugins = sorted(service.plugins.semantic.keys())
            self.active_reranker_plugins = sorted(service.plugins.rerankers.keys())
            ingested = self.ingest(service, item)
            context, included, metrics = self.build_context(service, item)
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
            **metrics,
        }
        self.results.append(record)
        return record

    def report(self) -> dict[str, Any]:
        by_type: dict[str, dict[str, int]] = {}
        recall_by_type: dict[str, list[bool]] = {}
        coverage_by_type: dict[str, list[float]] = {}
        context_tokens = 0
        haystack_tokens = 0
        for record in self.results:
            bucket = by_type.setdefault(
                record["question_type"], {"total": 0, "correct": 0}
            )
            bucket["total"] += 1
            bucket["correct"] += int(record["correct"])
            if record.get("retrieval_hit") is not None:
                recall_by_type.setdefault(record["question_type"], []).append(
                    bool(record["retrieval_hit"])
                )
            if record.get("gold_coverage") is not None:
                coverage_by_type.setdefault(record["question_type"], []).append(
                    float(record["gold_coverage"])
                )
            context_tokens += int(record.get("context_tokens") or 0)
            haystack_tokens += int(record.get("haystack_tokens") or 0)
        overall_total = sum(item["total"] for item in by_type.values())
        overall_correct = sum(item["correct"] for item in by_type.values())
        return {
            "benchmark": "LongMemEval-S",
            "harness": "joiny-mnemonic-longmemeval",
            "judge_prompts": "arXiv 2512.12818 Appendix A.4 (verbatim)",
            "config": {
                "context_budget_tokens": self.context_budget_tokens,
                "retrieval_limit": self.retrieval_limit,
                "packing": self.packing,
                "rerank": self.rerank,
                "ingest_mode": self.ingest_mode,
                "semantic_plugins": self.active_semantic_plugins,
                "reranker_plugins": self.active_reranker_plugins,
                "runner_command": list(self.runner.command),
            },
            "per_type": {
                name: {
                    **counts,
                    "accuracy": round(counts["correct"] / counts["total"], 4),
                    **(
                        {
                            "retrieval_recall": round(
                                sum(recall_by_type[name]) / len(recall_by_type[name]), 4
                            )
                        }
                        if recall_by_type.get(name) else {}
                    ),
                    **(
                        {
                            "gold_coverage_mean": round(
                                sum(coverage_by_type[name])
                                / len(coverage_by_type[name]), 4
                            )
                        }
                        if coverage_by_type.get(name) else {}
                    ),
                }
                for name, counts in sorted(by_type.items())
            },
            "overall": {
                "total": overall_total,
                "correct": overall_correct,
                "accuracy": round(overall_correct / overall_total, 4)
                if overall_total else 0.0,
            },
            "tokens": {
                "context_sent_total": context_tokens,
                "haystack_total": haystack_tokens,
                "savings_ratio": round(1 - context_tokens / haystack_tokens, 4)
                if haystack_tokens else None,
                "context_per_question_mean": round(
                    context_tokens / overall_total
                ) if overall_total else 0,
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
    parser.add_argument(
        "--packing", choices=["rank", "breadth"], default="rank",
        help="packet packing: greedy fused-rank order, or breadth-first "
        "(first fragment of each session, then depth backfill)",
    )
    parser.add_argument(
        "--rerank", action="store_true",
        help="cross-encoder rerank of the candidate pool "
        "(requires sentence-transformers)",
    )
    parser.add_argument(
        "--ingest", choices=["raw", "distill"], default="raw",
        help="raw: verbatim turns only; distill: LLM-extracted facts "
        "derived alongside the turns (A/B for the Phase B shape)",
    )
    parser.add_argument(
        "--distill-cache", default="benchmarks/distill-cache",
        help="disk cache for per-session distillations (sessions repeat "
        "across questions)",
    )
    parser.add_argument("--limit-questions", type=int, default=0)
    parser.add_argument(
        "--sample-per-type", type=int, default=0,
        help="deterministic stratified subset: first N questions of each "
        "type (for cheap config sweeps)",
    )
    parser.add_argument(
        "--only-type", default="",
        help="run only questions of this question_type (targeted probes)",
    )
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
    if args.only_type:
        items = [item for item in items if item.question_type == args.only_type]
    if args.sample_per_type:
        taken: dict[str, int] = {}
        sampled = []
        for item in items:
            if taken.get(item.question_type, 0) < args.sample_per_type:
                sampled.append(item)
                taken[item.question_type] = taken.get(item.question_type, 0) + 1
        items = sampled
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
        packing=args.packing,
        rerank=args.rerank,
        ingest_mode=args.ingest,
        distill_cache_dir=Path(args.distill_cache),
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
    from .report_signing import stamp_report

    report = stamp_report(
        report,
        artifacts={"per_question_jsonl": jsonl_path, "dataset": Path(args.dataset)},
    )
    (output_dir / "longmemeval-latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["overall"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
