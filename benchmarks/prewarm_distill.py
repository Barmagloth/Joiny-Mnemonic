"""Parallel pre-warm of the LongMemEval distill cache.

The harness distills sessions sequentially through one `claude -p` call at
a time (~20s each with CLI startup); 19k unique sessions would take days.
Distillation is embarrassingly parallel and the cache is content-addressed
(sha256 of session_id + transcript, same key as
LMEHarness._distill_session), so this script fills the cache with N
concurrent workers and the benchmark run then hits it.

Usage:
  python benchmarks/prewarm_distill.py <dataset.json> [--workers 8]
      [--sample-per-type 0] [--limit-questions 0]
      [--cache-dir benchmarks/distill-cache]

Pass the SAME subset flags you will pass to the benchmark run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from joiny_mnemonic.longmemeval import (  # noqa: E402
    LMEHarness,
    SubprocessLLMRunner,
    load_dataset,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--sample-per-type", type=int, default=0)
    parser.add_argument("--limit-questions", type=int, default=0)
    parser.add_argument("--only-type", default="")
    parser.add_argument("--cache-dir", default="benchmarks/distill-cache")
    parser.add_argument(
        "--runner-command",
        default='["python","benchmarks/runner_claude_code.py"]',
    )
    args = parser.parse_args()

    import json

    command = json.loads(args.runner_command)
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
    if args.limit_questions:
        items = items[: args.limit_questions]

    harness = LMEHarness(
        SubprocessLLMRunner(command),
        distill_cache_dir=Path(args.cache_dir),
    )
    # Dedupe by the harness's own cache key so each session distills once.
    import hashlib

    unique: dict[str, dict] = {}
    for item in items:
        for session in item.sessions:
            transcript = "\n".join(
                f"[{turn.get('role', 'user')}] {turn.get('content', '')}"
                for turn in session["turns"]
            )
            key = hashlib.sha256(
                (session["session_id"] + transcript).encode("utf-8")
            ).hexdigest()[:24]
            unique.setdefault(key, session)
    cache_dir = Path(args.cache_dir)
    pending = {
        key: session
        for key, session in unique.items()
        if not (cache_dir / f"{key}.json").exists()
    }
    print(
        f"questions={len(items)} unique_sessions={len(unique)} "
        f"cached={len(unique) - len(pending)} to_distill={len(pending)}",
        flush=True,
    )
    if not pending:
        return 0

    started = time.time()
    done = 0
    failures = 0
    empty = 0
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        futures = {
            pool.submit(harness._distill_session, session): key
            for key, session in pending.items()
        }
        for future in concurrent.futures.as_completed(futures):
            done += 1
            try:
                facts = future.result()
                if not facts:
                    empty += 1
            except Exception as exc:  # keep warming; report at the end
                failures += 1
                print(f"FAIL {futures[future]}: {exc}", flush=True)
            if done % 25 == 0 or done == len(futures):
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0.0
                remaining = (len(futures) - done) / rate if rate else 0.0
                print(
                    f"[{done}/{len(futures)}] {rate * 60:.1f}/min "
                    f"eta {remaining / 60:.0f}min empty={empty} "
                    f"failures={failures}",
                    flush=True,
                )
    print(
        f"done: {done} sessions, {empty} empty fact lists, "
        f"{failures} failures in {(time.time() - started) / 60:.1f}min",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
