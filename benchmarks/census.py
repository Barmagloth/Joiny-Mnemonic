"""Offline failure census of saved benchmark runs (no LLM).

Two modes.

Shallow (default): deterministically buckets every persisted question row
by SESSION-level coverage. Honest vocabulary (review 2026-07-17):
`gold_coverage` in the saved rows means "every gold session is represented
by AT LEAST ONE packed fragment" — it does NOT prove the supporting
passage was packed. Buckets therefore claim only that much:

  cov0         no gold session represented in the packet
  partial      some gold sessions unrepresented
  sessions_ok  all gold sessions represented (wrong answers here span
               passage-selection, packing, status presentation AND true
               reader failures — undifferentiated at this level)
  no_gold      abstention rows without gold sessions
  cov0_correct correct with zero gold sessions packed (audit flag)

Deep (--deep DATASET): for every wrong row of the first run, rebuilds the
store (raw ingest), re-runs retrieval and packing with the harness's own
code and the recorded config, and PERSISTS the evidence: candidate-pool
session ids, packed hit ids, packed gold-session fragment texts. Adds the
middle-step proxy the shallow census cannot see:

  answer_text_packed: yes | no | indeterminate
    - deterministic substring containment of the gold answer (and its
      parenthesized variants) in packed gold-session fragments, casefolded;
    - answers longer than 30 chars after normalization are judged
      non-extractive and marked indeterminate rather than overclaimed.

The deep artifact makes the retrieval/packing/passage split reproducible;
it re-runs on current code (recorded), not the run-day binary.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def shallow(path: Path) -> dict:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    buckets = defaultdict(list)
    per_type = defaultdict(lambda: defaultdict(int))
    for row in rows:
        coverage = row.get("gold_coverage")
        correct = bool(row.get("correct"))
        qid = row.get("question_id")
        qtype = row.get("question_type", "?")
        if coverage is None:
            bucket = "no_gold"
        elif coverage == 0:
            bucket = "cov0_correct" if correct else "cov0_wrong"
        elif coverage < 1:
            bucket = "partial_correct" if correct else "partial_wrong"
        else:
            bucket = "sessions_ok_correct" if correct else "sessions_ok_wrong"
        buckets[bucket].append(qid)
        per_type[qtype][bucket] += 1
    return {
        "run": str(path),
        "total": len(rows),
        "wrong": sum(1 for r in rows if not r.get("correct")),
        "buckets": {k: len(v) for k, v in sorted(buckets.items())},
        "bucket_ids": {
            k: v
            for k, v in sorted(buckets.items())
            if not k.endswith("_correct") or k == "cov0_correct"
        },
        "per_type": {t: dict(b) for t, b in sorted(per_type.items())},
    }


def _answer_variants(answer: str) -> list[str]:
    text = " ".join(str(answer).split())
    variants = [text]
    for m in re.findall(r"\(or ([^)]+)\)", text):
        variants.append(m.strip())
    stripped = re.sub(r"\([^)]*\)", "", text).strip()
    if stripped and stripped != text:
        variants.append(stripped)
    return [v for v in variants if len(v) >= 2]


def deep(run_path: Path, dataset: str, output_dir: Path) -> dict:
    from joiny_mnemonic.longmemeval import (
        LMEHarness, SubprocessLLMRunner, load_dataset,
    )
    from joiny_mnemonic.service import MemoryService

    rows = {
        r["question_id"]: r
        for line in run_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for r in [json.loads(line)]
    }
    wrong_ids = [q for q, r in rows.items() if not r.get("correct")]
    items = {
        q.question_id: q
        for q in load_dataset(dataset)
        if q.question_id in wrong_ids
    }
    config = {
        "budget": 12288, "retrieval_limit": 64, "packing": "rank",
        "ingest": "raw",
        "code_commit": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip(),
        "note": "re-run on current code, not the run-day binary",
    }
    harness = LMEHarness(
        SubprocessLLMRunner(["python", "-c", "pass"]),
        context_budget_tokens=config["budget"],
        retrieval_limit=config["retrieval_limit"],
        packing=config["packing"],
        ingest_mode="raw",
    )
    evidence = []
    counts = defaultdict(int)
    for index, qid in enumerate(wrong_ids, 1):
        item = items[qid]
        gold = set(item.answer_session_ids)
        service = MemoryService(":memory:", project_root=ROOT)
        try:
            harness.ingest(service, item)
            anchor = (
                f"{item.question_date}T12:00:00+00:00"
                if item.question_date else None
            )
            hits = service.search(
                query=item.question, limit=config["retrieval_limit"],
                include_events=True, record_telemetry=False,
                query_timestamp=anchor,
            )
            pool_sessions: dict[str, str] = {}
            for hit in hits:
                try:
                    event = service.store.get_event(hit.id)
                    pool_sessions[hit.id] = str(
                        event.payload.get("session_id") or ""
                    )
                except KeyError:
                    pool_sessions[hit.id] = ""
            context, included, metrics = harness.build_context(service, item)
            packed_gold_texts = []
            for hit_id in included:
                if pool_sessions.get(hit_id) in gold:
                    try:
                        packed_gold_texts.append(
                            service.store.get_event(hit_id).content
                        )
                    except KeyError:
                        pass
        finally:
            service.close()
        pool_gold = gold & set(pool_sessions.values())
        packed_sessions = {
            pool_sessions.get(h, "") for h in included
        } & gold
        haystack = " ".join(" ".join(t.split()) for t in packed_gold_texts).casefold()
        variants = _answer_variants(item.answer)
        extractive = all(len(v) <= 30 for v in variants[:1])
        # Aggregate answers (bare counts/sums) are computed, never quoted:
        # substring containment cannot judge them (manual audit 2026-07-17
        # found the len>=2 variant filter silently dumped every one-digit
        # count into passage:no). They get their own honest bucket.
        aggregate = bool(re.fullmatch(r"[\d,.$ ]{1,7}", " ".join(str(item.answer).split())))
        if not packed_gold_texts:
            answer_packed = "no_gold_fragments"
        elif aggregate:
            answer_packed = "aggregate"
        elif not extractive:
            answer_packed = "indeterminate"
        elif any(v.casefold() in haystack for v in variants):
            answer_packed = "yes"
        else:
            answer_packed = "no"
        stage = (
            "retrieval" if not pool_gold
            else "packing" if len(packed_sessions) < len(gold)
            else f"passage:{answer_packed}"
        )
        counts[stage] += 1
        evidence.append(
            {
                "question_id": qid,
                "question_type": item.question_type,
                "gold_sessions": sorted(gold),
                "pool_gold_sessions": sorted(pool_gold),
                "packed_gold_sessions": sorted(packed_sessions),
                "answer_variants": variants,
                "answer_text_packed": answer_packed,
                "stage": stage,
                "packed_gold_fragments": packed_gold_texts,
                "saved_gold_coverage": rows[qid].get("gold_coverage"),
            }
        )
        print(f"[{index}/{len(wrong_ids)}] {qid}: {stage}", flush=True)
    artifact = {
        "run": str(run_path),
        "config": config,
        "stages": dict(sorted(counts.items())),
        "evidence": evidence,
    }
    out = output_dir / "census-deep-latest.json"
    out.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("stages:", dict(sorted(counts.items())))
    print("written:", out)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--deep", default="", help="dataset path: rebuild "
                        "retrieval for the first run's failures and persist "
                        "pool/packed evidence")
    parser.add_argument("--output-dir", default="benchmarks/results")
    args = parser.parse_args()
    reports = [shallow(Path(p)) for p in args.runs]
    for rep in reports:
        print(f"== {rep['run']}  ({rep['total']} rows, {rep['wrong']} wrong)")
        print("   buckets:", rep["buckets"])
    out = Path(args.output_dir) / "census-latest.json"
    out.write_text(
        json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    if args.deep:
        deep(Path(args.runs[0]), args.deep, Path(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
