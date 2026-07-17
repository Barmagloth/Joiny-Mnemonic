"""Structural validation of an extraction eval corpus.

Every gold entry must survive the same rules the harness applies to an
extractor's output: the evidence quote occurs exactly once in the event
content, the declared zone matches the zone map, and normalized_content
is whitespace-normal. Run after any corpus edit.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from joiny_mnemonic.extraction import (  # noqa: E402
    ExtractionValidationError,
    locate_evidence,
    normalize_content,
)


def main() -> int:
    problems = 0
    for path in sys.argv[1:]:
        corpus = json.loads(Path(path).read_text(encoding="utf-8"))
        ids = Counter(item["id"] for item in corpus["examples"])
        for identifier, count in ids.items():
            if count > 1:
                print(f"{path}: duplicate id {identifier}")
                problems += 1
        type_counts: Counter = Counter()
        for item in corpus["examples"]:
            for expected in item.get("expected", ()):
                type_counts[expected["memory_type"]] += 1
                try:
                    _, _, zone = locate_evidence(
                        item["current"], expected["evidence_quote"]
                    )
                except ExtractionValidationError as exc:
                    print(f"{path}:{item['id']}: {exc.code}: {exc}")
                    problems += 1
                    continue
                declared = expected.get("evidence_zone", "prose")
                if zone != declared:
                    print(
                        f"{path}:{item['id']}: zone mismatch "
                        f"declared={declared} actual={zone}"
                    )
                    problems += 1
                if expected["normalized_content"] != normalize_content(
                    expected["normalized_content"]
                ):
                    print(
                        f"{path}:{item['id']}: normalized_content is not "
                        "whitespace-normal"
                    )
                    problems += 1
        print(
            f"{path}: {len(corpus['examples'])} examples, per-type positives "
            f"{dict(sorted(type_counts.items()))}"
        )
    print("PROBLEMS:", problems)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
