"""Classify the false positives of a stage 6 run, using the gate's own rule.

A claim like "every false positive is a trap example" is only worth something
if anyone can re-derive it. This reads the `--dump-predictions` file produced
by `stage6_extractor_eval.py`, re-applies the type-span matching rule and
labels each unmatched prediction:

    mistyped:<gold type>-><predicted type>   right span, wrong memory_type
    extra-on-empty-example                   gold is deliberately empty (a trap)
    adversarial-span                         gold empty AND flagged adversarial
    extra-span                               invented span on a scored example

It reads only the dump and the corpora; it never touches a report and cannot
change a decision.

    PYTHONPATH=src python benchmarks/stage6_classify_false_positives.py \
        dump.json classification.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joiny_mnemonic.extraction import locate_evidence  # noqa: E402

CORPORA = {
    "en": ROOT / "evals" / "extraction_en_v2.json",
    "ru": ROOT / "evals" / "extraction_ru_v2.json",
}


def _span(text: str, quote: str) -> tuple[int, int]:
    try:
        start, end, _ = locate_evidence(text, quote)
    except ValueError:
        return (-1, -1)
    return (start, end)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    overlap = min(a[1], b[1]) - max(a[0], b[0])
    shorter = min(a[1] - a[0], b[1] - b[0])
    return shorter > 0 and overlap / shorter >= 0.5


def classify(dump: dict) -> dict:
    findings: list[dict] = []
    counts: Counter = Counter()
    for language, rows in dump["languages"].items():
        corpus = json.loads(CORPORA[language].read_text(encoding="utf-8"))
        by_id = {item["id"]: item for item in corpus["examples"]}
        for row in rows:
            item = by_id[row["id"]]
            current = item["current"]
            remaining = [
                (value, _span(current, value["evidence_quote"]))
                for value in row["expected"]
            ]
            remaining = [pair for pair in remaining if pair[1] != (-1, -1)]
            for candidate in row["predicted"]:
                span = _span(current, candidate["evidence_quote"])
                hit = next(
                    (
                        position
                        for position, (value, gold) in enumerate(remaining)
                        if value["memory_type"] == candidate["memory_type"]
                        and _overlaps(span, gold)
                    ),
                    None,
                )
                if hit is not None:
                    remaining.pop(hit)
                    continue
                same_place = [
                    value["memory_type"]
                    for value, gold in remaining
                    if _overlaps(span, gold)
                ]
                if same_place:
                    kind = f"mistyped:{same_place[0]}->{candidate['memory_type']}"
                elif item.get("adversarial"):
                    kind = "adversarial-span"
                elif not row["expected"]:
                    kind = "extra-on-empty-example"
                else:
                    kind = "extra-span"
                counts[f"{language}/{kind}"] += 1
                counts[f"*/type:{candidate['memory_type']}"] += 1
                findings.append(
                    {
                        "language": language,
                        "id": row["id"],
                        "kind": kind,
                        "initial_status": candidate["initial_status"],
                        "memory_type": candidate["memory_type"],
                        "evidence_quote": candidate["evidence_quote"],
                        "normalized_content": candidate["normalized_content"],
                        "gold": [
                            {
                                "memory_type": value["memory_type"],
                                "evidence_quote": value["evidence_quote"],
                            }
                            for value in row["expected"]
                        ],
                    }
                )
    return {
        "identity_hash": dump.get("identity_hash"),
        "backend": dump.get("backend"),
        "summary": dict(sorted(counts.items())),
        "total_false_positives": len(findings),
        "false_positives": findings,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        raise SystemExit(
            "usage: stage6_classify_false_positives.py <dump.json> <out.json>"
        )
    dump = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    result = classify(dump)
    Path(argv[1]).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "total_false_positives": result["total_false_positives"],
                "summary": result["summary"],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
