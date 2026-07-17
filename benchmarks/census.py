"""Offline census of saved benchmark runs (no LLM).

Deterministically buckets every persisted question row by where the
pipeline lost it (user taxonomy 2026-07-17):

  cov0      gold sessions absent from the packet (retrieval OR packing -
            the saved rows record PACKED sessions, so splitting those two
            needs a local retrieval re-run for this subset only)
  partial   some but not all gold sessions packed
  full      all gold packed - a wrong answer here is a reader/synthesis
            (or status-presentation) failure, the last-mile class
  no_gold   abstention rows without gold sessions (skipped from coverage
            buckets, counted separately)
  leakage   correct answer with zero gold packed - either an allowed
            alternative source or judge leniency; flagged for manual audit

Usage: python benchmarks/census.py <run.jsonl> [<run.jsonl> ...]
Writes benchmarks/results/census-latest.json and prints the tables.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def census(path: Path) -> dict:
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
            bucket = "cov0_leakage" if correct else "cov0_wrong"
        elif coverage < 1:
            bucket = "partial_correct" if correct else "partial_wrong"
        else:
            bucket = "full_correct" if correct else "full_wrong"
        buckets[bucket].append(qid)
        per_type[qtype][bucket] += 1
    total = len(rows)
    wrong = sum(1 for r in rows if not r.get("correct"))
    return {
        "run": str(path),
        "total": total,
        "wrong": wrong,
        "buckets": {k: len(v) for k, v in sorted(buckets.items())},
        "bucket_ids": {
            k: v
            for k, v in sorted(buckets.items())
            if k in ("cov0_wrong", "partial_wrong", "full_wrong", "cov0_leakage")
        },
        "per_type": {t: dict(b) for t, b in sorted(per_type.items())},
    }


def main() -> int:
    reports = [census(Path(p)) for p in sys.argv[1:]]
    for rep in reports:
        print(f"== {rep['run']}  ({rep['total']} rows, {rep['wrong']} wrong)")
        print("   buckets:", rep["buckets"])
        for name in ("full_wrong", "partial_wrong", "cov0_wrong", "cov0_leakage"):
            ids = rep["bucket_ids"].get(name, [])
            if ids:
                print(f"   {name}: {ids}")
    out = Path("benchmarks/results/census-latest.json")
    out.write_text(
        json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("written:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
