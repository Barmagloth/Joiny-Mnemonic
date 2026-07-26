from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


PUBLIC_SURFACES = (
    "src/joiny_mnemonic/cli.py",
    "src/joiny_mnemonic/mcp.py",
    "src/joiny_mnemonic/api.py",
    "src/joiny_mnemonic/hooks.py",
)


@dataclass(frozen=True, slots=True)
class DirectStoreWrite:
    path: str
    line: int
    method: str


def declared_store_reads(root: Path) -> frozenset[str]:
    """Read the effective MemoryStore API instead of maintaining an allowlist."""
    source_root = str((root / "src").resolve())
    sys.path.insert(0, source_root)
    try:
        from joiny_mnemonic.storage import MemoryStore

        return frozenset(
            name
            for name in dir(MemoryStore)
            if getattr(
                getattr(MemoryStore, name, None), "_joiny_store_read_only", False
            )
        )
    finally:
        if sys.path and sys.path[0] == source_root:
            sys.path.pop(0)


def calls_in_source(
    source: str,
    *,
    path: str,
    read_methods: frozenset[str] = frozenset(),
) -> tuple[DirectStoreWrite, ...]:
    """Find direct store calls and escapes in one public-surface module."""
    tree = ast.parse(source, filename=path)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    violations: list[DirectStoreWrite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "store":
            continue
        member = parents.get(node)
        call = parents.get(member) if isinstance(member, ast.Attribute) else None
        if isinstance(member, ast.Attribute) and isinstance(call, ast.Call):
            if call.func is member and member.attr not in read_methods:
                violations.append(DirectStoreWrite(path, node.lineno, member.attr))
            continue
        method = (
            f"{member.attr}<store_escape>"
            if isinstance(member, ast.Attribute)
            else "<store_escape>"
        )
        violations.append(DirectStoreWrite(path, node.lineno, method))
    return tuple(
        sorted(violations, key=lambda item: (item.path, item.line, item.method))
    )


def direct_store_writes(
    root: Path, paths: Iterable[str] = PUBLIC_SURFACES
) -> tuple[DirectStoreWrite, ...]:
    read_methods = declared_store_reads(root)
    violations = [
        violation
        for relative in paths
        for violation in calls_in_source(
            (root / relative).read_text(encoding="utf-8"),
            path=relative,
            read_methods=read_methods,
        )
    ]
    return tuple(
        sorted(violations, key=lambda item: (item.path, item.line, item.method))
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    violations = direct_store_writes(args.root.resolve())
    if args.json:
        print(json.dumps([asdict(item) for item in violations], indent=2))
    else:
        for item in violations:
            print(f"{item.path}:{item.line}: direct store write {item.method}")
        print(f"direct store writes: {len(violations)}")
    return 1 if args.require_clean and violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
