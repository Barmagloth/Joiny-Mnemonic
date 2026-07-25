from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


PUBLIC_SURFACES = (
    "src/joiny_mnemonic/cli.py",
    "src/joiny_mnemonic/mcp.py",
    "src/joiny_mnemonic/api.py",
    "src/joiny_mnemonic/hooks.py",
)

REFLECTION_CALLS = frozenset({
    "delattr", "eval", "exec", "setattr", "vars",
})
SAFE_GETATTR = frozenset({("stream", "buffer"), ("stream", "reconfigure")})


@dataclass(frozen=True, slots=True)
class DirectStoreWrite:
    path: str
    line: int
    method: str


def _decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def declared_store_reads(root: Path) -> frozenset[str]:
    classes: dict[str, tuple[ast.ClassDef, str]] = {}
    for source_path in sorted((root / "src/joiny_mnemonic").glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        relative = source_path.relative_to(root).as_posix()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if node.name in classes:
                    raise ValueError(f"duplicate store class candidate: {node.name}")
                classes[node.name] = (node, relative)
    if "MemoryStore" not in classes:
        raise ValueError("MemoryStore declaration not found")
    mro_names: set[str] = set()
    pending = ["MemoryStore"]
    while pending:
        name = pending.pop()
        if name in mro_names:
            continue
        mro_names.add(name)
        node, _ = classes[name]
        for base in node.bases:
            base_name = _decorator_name(base)
            if base_name in classes:
                pending.append(str(base_name))
    declarations: dict[str, str] = {}
    for class_name in sorted(mro_names):
        node, relative = classes[class_name]
        for member in node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "store_read" not in {
                _decorator_name(item) for item in member.decorator_list
            }:
                continue
            if member.name in declarations:
                raise ValueError(
                    f"duplicate @store_read declaration {member.name}: "
                    f"{declarations[member.name]} and {relative}"
                )
            declarations[member.name] = relative
    return frozenset(declarations)


def calls_in_source(
    source: str, *, path: str, read_methods: frozenset[str] = frozenset()
) -> tuple[DirectStoreWrite, ...]:
    tree = ast.parse(source, filename=path)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    writes: list[DirectStoreWrite] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
        ):
            target = node.args[0].id if node.args and isinstance(node.args[0], ast.Name) else None
            attribute = (
                node.args[1].value
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                else None
            )
            if (target, attribute) not in SAFE_GETATTR:
                writes.append(
                    DirectStoreWrite(path, node.lineno, "<dynamic_access>")
                )
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in REFLECTION_CALLS
        ):
            writes.append(DirectStoreWrite(path, node.lineno, "<dynamic_access>"))
            continue
        if isinstance(node, ast.Attribute) and node.attr in {
            "__dict__", "__getattribute__"
        }:
            writes.append(DirectStoreWrite(path, node.lineno, "<dynamic_access>"))
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "store"
        ):
            writes.append(
                DirectStoreWrite(path, node.lineno, "<dynamic_store_access>")
            )
            continue
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "__dict__"
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "store"
        ):
            writes.append(
                DirectStoreWrite(path, node.lineno, "<dynamic_store_access>")
            )
            continue
        if not isinstance(node, ast.Attribute) or node.attr != "store":
            continue
        parent = parents.get(node)
        if not isinstance(parent, ast.Attribute) or parent.value is not node:
            writes.append(DirectStoreWrite(path, node.lineno, "<store_escape>"))
            continue
        grandparent = parents.get(parent)
        if isinstance(grandparent, ast.Call) and grandparent.func is parent:
            if parent.attr not in read_methods:
                writes.append(DirectStoreWrite(path, node.lineno, parent.attr))
            continue
        writes.append(
            DirectStoreWrite(path, node.lineno, f"{parent.attr}<store_escape>")
        )
    return tuple(sorted(writes, key=lambda item: (item.path, item.line, item.method)))


def direct_store_writes(
    root: Path, paths: Iterable[str] = PUBLIC_SURFACES
) -> tuple[DirectStoreWrite, ...]:
    read_methods = declared_store_reads(root)
    writes: list[DirectStoreWrite] = []
    for relative in paths:
        source = (root / relative).read_text(encoding="utf-8")
        writes.extend(calls_in_source(source, path=relative, read_methods=read_methods))
    return tuple(sorted(writes, key=lambda item: (item.path, item.line, item.method)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    writes = direct_store_writes(args.root.resolve())
    if args.json:
        print(json.dumps([asdict(item) for item in writes], indent=2))
    else:
        for item in writes:
            print(f"{item.path}:{item.line}: direct store write {item.method}")
        print(f"direct store writes: {len(writes)}")
    return 1 if args.require_clean and writes else 0


if __name__ == "__main__":
    raise SystemExit(main())
