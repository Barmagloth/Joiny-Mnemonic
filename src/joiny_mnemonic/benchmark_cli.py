from __future__ import annotations

import argparse
import json
from pathlib import Path

from .benchmarking import run_benchmark, write_report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="joiny-mnemonic-benchmark")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--output", default="benchmarks/results")
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--prompt-exposures", type=int, default=10)
    parser.add_argument("--token-model", default="gpt-4o")
    parser.add_argument("--assert-gates", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()
    report = run_benchmark(
        root,
        reduction_repetitions=args.repetitions,
        prompt_exposures=args.prompt_exposures,
        token_model=args.token_model,
    )
    json_path, markdown_path = write_report(report, root / args.output)
    print(json.dumps({
        "passed": report["passed"],
        "gates": report["gates"],
        "aggregate": report["aggregate"],
        "json": str(json_path),
        "markdown": str(markdown_path),
    }, ensure_ascii=False, indent=2))
    if args.assert_gates and not report["passed"]:
        raise SystemExit(1)