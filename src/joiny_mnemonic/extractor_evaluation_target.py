"""Stage 6 evaluation identity and gate (JM-INV-007).

``PASSED`` may only be computed for a system that exactly matches a target
frozen *before* the run. Everything a result depends on is part of that target:
the corpus bytes, the extractor name and version, the model and its exact
revision, the inference settings, the prompt hash, the schema hash and the
version of the checking code. A run against anything else is not a weaker
result — it is a result about a different system, and the gate refuses it.

Reports are append-only artifacts: a dated report is never rewritten with
different content, and ``latest`` is a pointer to one of them, never a
substitute for the historical file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .report_signing import canonical_json


CHECKER_VERSION = "stage6-extractor-gate-v2"

#: Pre-registered stage 6 thresholds (ROADMAP §9).
DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_precision": 0.90,
    "min_recall": 0.70,
    "max_language_precision_gap": 0.10,
    "max_false_trusted": 0,
}


class EvaluationIdentityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def corpus_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationTarget:
    """The exact system a report is allowed to speak about."""

    extractor_name: str
    extractor_version: str
    extractor_config_hash: str
    backend: Mapping[str, Any]
    corpus_digests: Mapping[str, str]
    #: Which memory types the thresholds are applied to. Recorded rather than
    #: implied: a gate over one type does not license a cross-type claim.
    scored_types: tuple[str, ...] = ("preference",)
    thresholds: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_THRESHOLDS)
    )
    checker_version: str = CHECKER_VERSION

    def descriptor(self) -> dict[str, Any]:
        return {
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "extractor_config_hash": self.extractor_config_hash,
            "backend": dict(self.backend),
            "corpus_digests": dict(self.corpus_digests),
            "scored_types": list(self.scored_types),
            "thresholds": dict(self.thresholds),
            "checker_version": self.checker_version,
        }

    @property
    def identity_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.descriptor()).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_descriptor(cls, value: Mapping[str, Any]) -> "EvaluationTarget":
        missing = {
            "extractor_name",
            "extractor_version",
            "extractor_config_hash",
            "backend",
            "corpus_digests",
        } - set(value)
        if missing:
            raise EvaluationIdentityError(
                "incomplete_target",
                f"frozen target is missing: {', '.join(sorted(missing))}",
            )
        return cls(
            extractor_name=str(value["extractor_name"]),
            extractor_version=str(value["extractor_version"]),
            extractor_config_hash=str(value["extractor_config_hash"]),
            backend=dict(value["backend"]),
            corpus_digests=dict(value["corpus_digests"]),
            scored_types=tuple(value.get("scored_types", ("preference",))),
            thresholds=dict(value.get("thresholds", DEFAULT_THRESHOLDS)),
            checker_version=str(value.get("checker_version", CHECKER_VERSION)),
        )


def load_target(path: str | Path) -> EvaluationTarget:
    return EvaluationTarget.from_descriptor(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def target_mismatches(
    frozen: EvaluationTarget, actual: EvaluationTarget
) -> list[str]:
    """Field-by-field difference; an empty list is the only licence to pass."""
    problems: list[str] = []
    expected = frozen.descriptor()
    observed = actual.descriptor()
    for key in sorted(expected):
        if expected[key] != observed.get(key):
            problems.append(
                f"{key}: frozen {canonical_json(expected[key])} != "
                f"actual {canonical_json(observed.get(key))}"
            )
    return problems


def _scored(report: Mapping[str, Any], scored_types: tuple[str, ...]) -> dict[str, float]:
    """Aggregate the scored types by summing their confusion counts."""
    tp = fp = fn = 0
    for memory_type in scored_types:
        scores = report.get("by_memory_type", {}).get(memory_type)
        if not scores:
            continue
        tp += int(scores["true_positive"])
        fp += int(scores["false_positive"])
        fn += int(scores["false_negative"])
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "false_trusted": int(report.get("false_trusted_records", 0)),
    }


def evaluate_gate(
    target: EvaluationTarget, reports: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Apply the frozen thresholds; every language is judged separately."""
    thresholds = dict(target.thresholds)
    rows = {
        language: _scored(report, target.scored_types)
        for language, report in reports.items()
    }
    if not rows:
        raise EvaluationIdentityError(
            "no_language_reports", "the gate requires at least one language report"
        )
    # Every language named in the frozen target must be measured. A run that
    # reports only the language a model happens to be good at is not a weaker
    # result — it is a result about a different, smaller system, and the
    # per-language thresholds exist precisely to forbid that trade.
    expected_languages = set(target.corpus_digests)
    missing = sorted(expected_languages - set(rows))
    unexpected = sorted(set(rows) - expected_languages)
    if missing or unexpected:
        raise EvaluationIdentityError(
            "language_coverage_mismatch",
            "the gate requires exactly the languages of the frozen target; "
            f"missing: {missing or 'none'}, unexpected: {unexpected or 'none'}",
        )
    checks = {}
    for language, row in sorted(rows.items()):
        checks[f"precision_{language}"] = (
            row["precision"] >= thresholds["min_precision"]
        )
        checks[f"recall_{language}"] = row["recall"] >= thresholds["min_recall"]
    precisions = [row["precision"] for row in rows.values()]
    checks["language_precision_gap"] = (
        max(precisions) - min(precisions)
    ) <= thresholds["max_language_precision_gap"]
    checks["false_trusted"] = sum(
        row["false_trusted"] for row in rows.values()
    ) <= thresholds["max_false_trusted"]
    return {"rows": rows, "checks": checks, "thresholds_met": all(checks.values())}


def decide(
    frozen: EvaluationTarget,
    actual: EvaluationTarget,
    reports: Mapping[str, Mapping[str, Any]],
    *,
    worktree_clean: bool | None,
) -> dict[str, Any]:
    """The only place ``passed`` is computed.

    ``worktree_clean`` is required, not defaulted, and an unknown state counts
    as dirty. A ``PASSED`` produced from uncommitted code names a system that
    exists only on one machine, which is exactly what JM-INV-007 forbids; the
    provenance block records the same fact, but recording is not refusing.
    """
    mismatches = target_mismatches(frozen, actual)
    gate = evaluate_gate(actual, reports)
    clean = worktree_clean is True
    return {
        "frozen_identity_hash": frozen.identity_hash,
        "actual_identity_hash": actual.identity_hash,
        "identity_mismatches": mismatches,
        "identity_matches": not mismatches,
        "worktree_clean": clean,
        "gate": gate,
        "passed": (not mismatches) and gate["thresholds_met"] and clean,
    }


def write_report(
    directory: str | Path, filename: str, report: Mapping[str, Any]
) -> Path:
    """Write a dated report once; never silently replace a differing one."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing != payload:
            raise EvaluationIdentityError(
                "report_would_be_rewritten",
                f"{target.name} already exists with different content; a re-run "
                "produces a new dated report and never edits a published one",
            )
        return target
    target.write_text(payload, encoding="utf-8")
    return target


def write_latest_pointer(directory: str | Path, report_path: str | Path) -> Path:
    """``latest`` cites a historical report; it never stands in for one."""
    root = Path(directory)
    report = Path(report_path)
    body = json.loads(report.read_text(encoding="utf-8"))
    pointer = {
        "schema": "joiny-mnemonic-stage6-latest-pointer-v1",
        "report": report.name,
        "report_sha256": body.get("report_sha256"),
        "identity_hash": body.get("decision", {}).get("actual_identity_hash"),
        "passed": body.get("decision", {}).get("passed"),
    }
    path = root / "latest.json"
    path.write_text(
        json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
