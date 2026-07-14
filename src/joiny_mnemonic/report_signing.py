"""Provenance stamping for benchmark reports.

A benchmark number without provenance is an anecdote. Every report this
project publishes carries a ``provenance`` block (what code produced it,
from which commit, over which artifacts) and a ``report_sha256`` computed
over the canonical JSON of everything else. Verification is offline and
zero-dependency, in line with the rest of the core.

This is tamper *evidence*, not tamper *proof*: the hash pins the report to
its stated provenance so silent edits are detectable; it is not a
cryptographic signature by a private key.

Usage as a module:
    python -m joiny_mnemonic.report_signing stamp report.json [--artifact f]
    python -m joiny_mnemonic.report_signing verify report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .storage import (
    CURRENT_SCHEMA_VERSION,
    SNAPSHOT_REPLAY_CODE_VERSION,
)
from .temporal import TEMPORAL_PROJECTION_CODE_VERSION

REPORT_SIGNING_VERSION = "report-signing-v1"


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _package_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("joiny-mnemonic")
    except Exception:
        return None


def stamp_report(
    report: dict[str, Any],
    *,
    repo_root: Path | None = None,
    artifacts: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Return the report with a fresh provenance block and content hash.

    ``artifacts`` maps a label to a file whose bytes back the report (e.g.
    the per-question JSONL); each is pinned by its own sha256 so the summary
    cannot drift from its underlying rows unnoticed.
    """
    root = (repo_root or Path.cwd()).resolve()
    stamped = {
        key: value
        for key, value in report.items()
        if key not in ("provenance", "report_sha256")
    }
    artifact_hashes = {}
    for label, path in sorted((artifacts or {}).items()):
        artifact_hashes[label] = hashlib.sha256(
            Path(path).read_bytes()
        ).hexdigest()
    # Tracked-file modifications only, and only to CODE: the report being
    # stamped (and its sibling artifacts under benchmarks/results/) are
    # products of the run — a report cannot avoid "modifying" itself, so
    # counting it would make every stamp dirty by construction.
    porcelain = _git(["status", "--porcelain", "--untracked-files=no"], root)
    dirty = None
    if porcelain is not None:
        code_changes = [
            line
            for line in porcelain.splitlines()
            if line.strip()
            and "benchmarks/results/" not in line.replace("\\", "/")
        ]
        dirty = "\n".join(code_changes)
    stamped["provenance"] = {
        "signing_version": REPORT_SIGNING_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": _git(["rev-parse", "HEAD"], root),
        "git_dirty": bool(dirty) if dirty is not None else None,
        "package_version": _package_version(),
        "schema_version": CURRENT_SCHEMA_VERSION,
        "temporal_projection_code_version": TEMPORAL_PROJECTION_CODE_VERSION,
        "snapshot_replay_code_version": SNAPSHOT_REPLAY_CODE_VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "artifact_sha256": artifact_hashes,
    }
    stamped["report_sha256"] = hashlib.sha256(
        canonical_json(stamped).encode("utf-8")
    ).hexdigest()
    return stamped


def verify_report(
    report: dict[str, Any], *, artifacts: dict[str, Path] | None = None
) -> tuple[bool, list[str]]:
    """Recompute the content hash (and artifact hashes when the files are
    supplied); returns (ok, list of mismatches)."""
    problems: list[str] = []
    claimed = report.get("report_sha256")
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    actual = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    if claimed != actual:
        problems.append(f"report_sha256 mismatch: claimed {claimed}, actual {actual}")
    recorded = report.get("provenance", {}).get("artifact_sha256", {})
    for label, path in sorted((artifacts or {}).items()):
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if recorded.get(label) != digest:
            problems.append(
                f"artifact '{label}' sha256 mismatch: recorded "
                f"{recorded.get(label)}, actual {digest}"
            )
    return (not problems, problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="joiny-mnemonic-report-signing")
    commands = parser.add_subparsers(dest="command", required=True)
    stamp = commands.add_parser("stamp")
    stamp.add_argument("report", help="JSON report file to stamp in place")
    stamp.add_argument(
        "--artifact", action="append", default=[],
        help="label=path of a backing artifact to pin (repeatable)",
    )
    verify = commands.add_parser("verify")
    verify.add_argument("report")
    verify.add_argument("--artifact", action="append", default=[])
    args = parser.parse_args(argv)

    path = Path(args.report)
    artifacts = {}
    for item in args.artifact:
        label, _, target = item.partition("=")
        artifacts[label] = Path(target)
    report = json.loads(path.read_text(encoding="utf-8"))
    if args.command == "stamp":
        stamped = stamp_report(report, artifacts=artifacts)
        path.write_text(
            json.dumps(stamped, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(
            {"report_sha256": stamped["report_sha256"],
             "git_commit": stamped["provenance"]["git_commit"]},
            ensure_ascii=False,
        ))
        return 0
    ok, problems = verify_report(report, artifacts=artifacts)
    print(json.dumps({"ok": ok, "problems": problems}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
