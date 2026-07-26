"""Stage 6: measure one extractor candidate through the model-agnostic connector.

Every candidate is measured by this one pipeline. The model is selected by
configuration, so `qwen3-4b`, `gemma-3-4b` and any other local runtime are the
same code path with a different frozen identity.

Freeze the target before the run, then measure:

    PYTHONPATH=src python benchmarks/stage6_extractor_eval.py \
        --backend backends/qwen3-4b.json --freeze targets/qwen3-4b.json
    PYTHONPATH=src python benchmarks/stage6_extractor_eval.py \
        --backend backends/qwen3-4b.json --target targets/qwen3-4b.json

`--limit N` runs the cheap smoke slice first (reachability, JSON validity,
empty-output rate, latency) before spending a full run.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joiny_mnemonic.extraction import ExtractorConfig  # noqa: E402
from joiny_mnemonic.extraction_evaluation import evaluate_extractor  # noqa: E402
from joiny_mnemonic.extractor_backend import validate_backend  # noqa: E402
from joiny_mnemonic.extractor_evaluation_target import (  # noqa: E402
    CHECKER_VERSION,
    DEFAULT_THRESHOLDS,
    EvaluationTarget,
    corpus_digest,
    decide,
    load_target,
    write_latest_pointer,
    write_report,
)
from joiny_mnemonic.plugins import PluginRegistry  # noqa: E402
from joiny_mnemonic.report_signing import stamp_report  # noqa: E402


CORPORA = {"en": "extraction_en_v2", "ru": "extraction_ru_v2"}


def _corpus_path(name: str, limit: int) -> Path:
    path = ROOT / "evals" / f"{name}.json"
    if not limit:
        return path
    trimmed = json.loads(path.read_text(encoding="utf-8"))
    trimmed["examples"] = trimmed["examples"][:limit]
    tmp = Path(tempfile.gettempdir()) / f"{name}-limit{limit}.json"
    tmp.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
    return tmp


def _smoke(rows: list[dict]) -> dict[str, float | int]:
    """Cheap health signals that decide whether a full run is worth paying for."""
    parse_errors = sum(1 for row in rows if "parse_error" in row)
    empty = sum(1 for row in rows if not row.get("predicted"))
    return {
        "examples": len(rows),
        "json_invalid": parse_errors,
        "json_validity_rate": 1 - parse_errors / len(rows) if rows else 0.0,
        "empty_output": empty,
        "empty_output_rate": empty / len(rows) if rows else 0.0,
    }


def _load_extractor(name: str):
    registry = PluginRegistry()
    plugin = registry.extractors.get(name)
    if plugin is None:
        available = ", ".join(sorted(registry.extractors)) or "none installed"
        raise SystemExit(
            f"extractor plugin {name!r} is not installed; available: {available}"
        )
    return plugin


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend", required=True, help="JSON file holding the backend block"
    )
    parser.add_argument("--extractor", default="local-llm")
    parser.add_argument(
        "--extractor-version", default="connector-v1", help="version of the plugin"
    )
    parser.add_argument("--target", help="frozen target file to measure against")
    parser.add_argument("--freeze", help="write the frozen target file and exit")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--scored-type", action="append", default=[])
    parser.add_argument("--output-dir", default="benchmarks/results/stage6")
    args = parser.parse_args()

    if bool(args.target) == bool(args.freeze):
        raise SystemExit("pass exactly one of --target or --freeze")

    backend = validate_backend(json.loads(Path(args.backend).read_text("utf-8")))
    config = ExtractorConfig.for_backend(backend)
    paths = {
        language: _corpus_path(name, args.limit)
        for language, name in CORPORA.items()
    }
    actual = EvaluationTarget(
        extractor_name=args.extractor,
        extractor_version=args.extractor_version,
        extractor_config_hash=config.canonical_hash,
        backend=backend.descriptor(),
        corpus_digests={
            language: corpus_digest(path) for language, path in paths.items()
        },
        scored_types=tuple(args.scored_type or ("preference",)),
        thresholds=dict(DEFAULT_THRESHOLDS),
        checker_version=CHECKER_VERSION,
    )

    if args.freeze:
        frozen_path = Path(args.freeze)
        frozen_path.parent.mkdir(parents=True, exist_ok=True)
        frozen_path.write_text(
            json.dumps(actual.descriptor(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"frozen": str(frozen_path), "identity_hash": actual.identity_hash}))
        return 0

    frozen = load_target(args.target)
    extractor = _load_extractor(args.extractor)
    reports: dict[str, dict] = {}
    smoke: dict[str, dict] = {}
    exact: dict[str, dict] = {}
    for language, path in paths.items():
        rows: list[dict] = []
        reports[language] = evaluate_extractor(
            extractor, config, path, match_mode="type-span", per_example_sink=rows
        )
        exact[language] = evaluate_extractor(
            extractor, config, path, match_mode="exact-triple"
        )
        smoke[language] = _smoke(rows)
        print(
            f"[{language}] smoke={smoke[language]} "
            f"latency_ms={reports[language]['latency_ms']}",
            flush=True,
        )

    decision = decide(frozen, actual, reports)
    report = stamp_report(
        {
            "schema": "joiny-mnemonic-stage6-extractor-eval-v1",
            "frozen_target": frozen.descriptor(),
            "actual_target": actual.descriptor(),
            "decision": decision,
            "smoke": smoke,
            "reports": reports,
            "exact_triple_reports": exact,
            "limited_to": args.limit or None,
            "candidate_only": True,
        },
        repo_root=ROOT,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    name = (
        f"extractor-eval-{stamp}-{backend.model}-"
        f"{actual.identity_hash[:12]}.json"
    )
    written = write_report(ROOT / args.output_dir, name, report)
    write_latest_pointer(ROOT / args.output_dir, written)
    print(
        json.dumps(
            {
                "report": str(written),
                "passed": decision["passed"],
                "identity_matches": decision["identity_matches"],
                "identity_mismatches": decision["identity_mismatches"],
                "checks": decision["gate"]["checks"],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0 if decision["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
