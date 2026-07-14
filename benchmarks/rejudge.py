"""Re-judge persisted LongMemEval answers with a different judge model.

Methodology-hardening tool (review 2026-07-15): the signed run judges with
the same stack that answers. This script replays ONLY the judging over the
per-question JSONL — answers untouched — so judge-model sensitivity is
measured without re-running the benchmark.

    set LME_MODEL=opus
    python benchmarks/rejudge.py benchmarks/results/longmemeval-latest.jsonl \
        R:/Projects/data/longmemeval_s_cleaned.json rejudge-opus.json

Writes a small report (overall/per-type accuracy under the new judge, and
every verdict flip) next to nothing — the signed artifact is never modified.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from joiny_mnemonic.longmemeval import (  # noqa: E402
    SubprocessLLMRunner,
    judge_prompt_for,
    load_dataset,
    parse_boxed,
)


def main() -> int:
    rows_path, dataset_path, output_path = sys.argv[1], sys.argv[2], sys.argv[3]
    rows = [
        json.loads(line)
        for line in Path(rows_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    questions = {item.question_id: item for item in load_dataset(dataset_path)}
    runner = SubprocessLLMRunner(
        ["python", str(Path(__file__).parent / "runner_claude_code.py")]
    )
    flips = []
    by_type: dict[str, list[int]] = {}
    for index, row in enumerate(rows, 1):
        item = questions[row["question_id"]]
        prompt = judge_prompt_for(item.question_id, item.question_type).format(
            question=item.question, answer=item.answer, response=row["answer"]
        )
        verdict_text = runner.call(
            {"mode": "judge", "prompt": prompt, "question_id": item.question_id}
        )
        verdict = bool(parse_boxed(verdict_text))
        by_type.setdefault(item.question_type, []).append(int(verdict))
        if verdict != row["correct"]:
            flips.append(
                {
                    "question_id": row["question_id"],
                    "question_type": item.question_type,
                    "original": row["correct"],
                    "rejudged": verdict,
                }
            )
        print(
            f"[{index}/{len(rows)}] {row['question_id']} "
            f"{'OK' if verdict else 'MISS'}"
            + (" FLIP" if verdict != row["correct"] else ""),
            flush=True,
        )
    total = sum(len(v) for v in by_type.values())
    correct = sum(sum(v) for v in by_type.values())
    report = {
        "tool": "rejudge",
        "source_rows": rows_path,
        "overall": {
            "total": total,
            "correct": correct,
            "accuracy": round(correct / total, 4),
        },
        "per_type": {
            name: {
                "total": len(values),
                "correct": sum(values),
                "accuracy": round(sum(values) / len(values), 4),
            }
            for name, values in sorted(by_type.items())
        },
        "flips": flips,
        "flip_count": len(flips),
    }
    Path(output_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["overall"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
