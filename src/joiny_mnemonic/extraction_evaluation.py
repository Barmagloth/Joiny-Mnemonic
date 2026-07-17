from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .extraction import locate_evidence, parse_candidates, validate_candidate
from .models import Event


def _event(index: int, item: dict[str, Any]) -> Event:
    return Event(
        seq=index + 1,
        id=f"eval_{index:04d}",
        branch_id="main",
        session_id="eval",
        kind=item.get("kind", "message"),
        role=item.get("role", "user"),
        origin_channel="public_api",
        origin_adapter=None,
        content=item["current"],
        payload={},
        files=tuple(item.get("files", ())),
        created_at="2026-01-01T00:00:00+00:00",
        previous_hash=None,
        content_hash="eval",
        chain_hash=f"eval-{index}",
    )


def _scores(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def evaluate_extractor(
    extractor: Any,
    config: Any,
    corpus_path: str | Path,
    *,
    match_mode: str = "exact-triple",
    per_example_sink: list | None = None,
) -> dict[str, Any]:
    """match_mode:
    - "exact-triple" (default, historical): TP requires exact equality of
      (memory_type, normalized_content, evidence_quote) — measures typing
      AND normalization calligraphy together.
    - "type-span": TP requires the same memory_type and an evidence span
      overlapping the gold span by >= 50% of the shorter span — measures
      typing quality proper (the 2a gate question: is a preference labelled
      a preference for the right piece of text), while quote validity is
      still enforced upstream by validate_candidate.
    """
    if match_mode not in ("exact-triple", "type-span"):
        raise ValueError(f"unsupported match_mode: {match_mode}")
    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    totals = [0, 0, 0]
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    by_zone: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    exact_attempted = 0
    exact_accepted = 0
    quarantined = 0
    false_trusted = 0
    latencies: list[float] = []
    observed_keys: list[tuple[str, str]] = []

    for index, item in enumerate(corpus["examples"]):
        if per_example_sink is not None:
            per_example_sink.append(
                {
                    "id": item["id"],
                    "adversarial": bool(item.get("adversarial")),
                    "expected": list(item.get("expected", ())),
                    "predicted": [],  # filled below
                }
            )
        event = _event(index, item)
        context = tuple(
            Event(
                **{
                    **asdict(event),
                    "seq": event.seq - len(item.get("context", ())) + position,
                    "id": f"{event.id}_ctx_{position}",
                    "content": content,
                }
            )
            for position, content in enumerate(item.get("context", ()))
        )
        started = time.perf_counter()
        raw = extractor.extract(event, context=context, config=config.descriptor())
        latencies.append((time.perf_counter() - started) * 1000)
        try:
            proposed = parse_candidates(raw)
        except ValueError as exc:
            # Fail-safe, mirroring the production path: malformed extractor
            # output contributes zero predictions for this example (its
            # golds become misses), never a crashed evaluation.
            proposed = ()
            if per_example_sink is not None:
                per_example_sink[-1]["parse_error"] = str(exc)
        predicted = []
        for candidate in proposed:
            exact_attempted += 1
            try:
                valid = validate_candidate(
                    candidate, event, threshold=config.auto_threshold
                )
            except ValueError:
                continue
            exact_accepted += 1
            quarantined += valid.initial_status == "quarantined"
            predicted.append(valid)
            observed_keys.append((valid.memory_type, valid.normalized_content.casefold()))
        if per_example_sink is not None:
            per_example_sink[-1]["predicted"] = [
                {
                    "memory_type": value.memory_type,
                    "normalized_content": value.normalized_content,
                    "evidence_quote": value.evidence_quote,
                    "evidence_zone": value.evidence_zone,
                    "initial_status": value.initial_status,
                    "confidence": value.confidence,
                }
                for value in predicted
            ]

        if match_mode == "exact-triple":
            expected = {
                (
                    value["memory_type"],
                    value["normalized_content"],
                    value["evidence_quote"],
                ): value
                for value in item.get("expected", ())
            }
            actual = {
                (
                    value.memory_type,
                    value.normalized_content,
                    value.evidence_quote,
                ): value
                for value in predicted
            }
            for key in actual.keys() & expected.keys():
                totals[0] += 1
                by_type[key[0]][0] += 1
                by_zone[expected[key].get("evidence_zone", "prose")][0] += 1
            for key in actual.keys() - expected.keys():
                totals[1] += 1
                by_type[key[0]][1] += 1
                by_zone[actual[key].evidence_zone][1] += 1
                if actual[key].initial_status == "auto" and item.get("adversarial"):
                    false_trusted += 1
            for key in expected.keys() - actual.keys():
                totals[2] += 1
                by_type[key[0]][2] += 1
                by_zone[expected[key].get("evidence_zone", "prose")][2] += 1
        else:  # type-span
            remaining = []
            for value in item.get("expected", ()):
                try:
                    gold_start, gold_end, _ = locate_evidence(
                        item["current"], value["evidence_quote"]
                    )
                except ValueError:
                    continue
                remaining.append((value, gold_start, gold_end))
            for candidate in predicted:
                match_index = -1
                for position, (value, gold_start, gold_end) in enumerate(remaining):
                    if value["memory_type"] != candidate.memory_type:
                        continue
                    overlap = min(candidate.evidence_end, gold_end) - max(
                        candidate.evidence_start, gold_start
                    )
                    shorter = min(
                        candidate.evidence_end - candidate.evidence_start,
                        gold_end - gold_start,
                    )
                    if shorter > 0 and overlap / shorter >= 0.5:
                        match_index = position
                        break
                if match_index >= 0:
                    value, _, _ = remaining.pop(match_index)
                    totals[0] += 1
                    by_type[candidate.memory_type][0] += 1
                    by_zone[value.get("evidence_zone", "prose")][0] += 1
                else:
                    totals[1] += 1
                    by_type[candidate.memory_type][1] += 1
                    by_zone[candidate.evidence_zone][1] += 1
                    if candidate.initial_status == "auto" and item.get("adversarial"):
                        false_trusted += 1
            for value, _, _ in remaining:
                if item.get("adversarial"):
                    # Adversarial traps measure false_trusted, not recall:
                    # refusing to extract from an injection line is correct
                    # behavior, never a miss.
                    continue
                totals[2] += 1
                by_type[value["memory_type"]][2] += 1
                by_zone[value.get("evidence_zone", "prose")][2] += 1

    unique = len(set(observed_keys))
    duplicates = len(observed_keys) - unique
    return {
        "corpus_version": corpus["version"],
        "match_mode": match_mode,
        "examples": len(corpus["examples"]),
        "overall": _scores(*totals),
        "by_memory_type": {
            key: _scores(*value) for key, value in sorted(by_type.items())
        },
        "by_evidence_zone": {
            key: _scores(*value) for key, value in sorted(by_zone.items())
        },
        "exact_evidence_acceptance_rate": (
            exact_accepted / exact_attempted if exact_attempted else 1.0
        ),
        "false_trusted_records": false_trusted,
        "quarantine_rate": quarantined / exact_accepted if exact_accepted else 0.0,
        "duplicate_rate": duplicates / len(observed_keys) if observed_keys else 0.0,
        "latency_ms": {
            "mean": sum(latencies) / len(latencies) if latencies else 0.0,
            "max": max(latencies, default=0.0),
        },
        "automatic_enablement_allowed": False,
        "threshold_under_evaluation": config.auto_threshold,
    }
