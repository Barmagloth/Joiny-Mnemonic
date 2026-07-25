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

REFLECTION_NAMES = {
    "delattr": "builtins.delattr",
    "getattr": "builtins.getattr",
    "setattr": "builtins.setattr",
    "vars": "builtins.vars",
}


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


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _resolved_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    name = _qualified_name(node)
    if name is None:
        return None
    if name in aliases:
        return aliases[name]
    head, separator, tail = name.partition(".")
    resolved_head = aliases.get(head)
    if separator and resolved_head:
        return f"{resolved_head}.{tail}"
    return name


def _reflection_aliases(tree: ast.AST) -> dict[str, str]:
    aliases = dict(REFLECTION_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"builtins", "operator"}:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            resolved = _resolved_name(assignment.value, aliases)
            if resolved not in {
                "builtins.delattr", "builtins.getattr", "builtins.setattr",
                "builtins.vars", "operator.attrgetter",
            }:
                continue
            for target in assignment.targets:
                if isinstance(target, ast.Name) and aliases.get(target.id) != resolved:
                    aliases[target.id] = resolved
                    changed = True
    return aliases


def _owner_aliases(tree: ast.AST) -> frozenset[str]:
    owners = {"service"}
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            if not isinstance(assignment.value, ast.Name) or assignment.value.id not in owners:
                continue
            for target in assignment.targets:
                if isinstance(target, ast.Name) and target.id not in owners:
                    owners.add(target.id)
                    changed = True
    return frozenset(owners)


def _constant_string(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_owner(node: ast.expr, owners: frozenset[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in owners


def _is_store_expression(
    node: ast.expr, aliases: dict[str, str], owners: frozenset[str]
) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "store":
        return True
    if isinstance(node, ast.Call):
        resolved = _resolved_name(node.func, aliases)
        if resolved == "builtins.getattr" and node.args:
            attribute = _constant_string(node.args[1]) if len(node.args) >= 2 else None
            return (
                attribute == "store"
                or _is_store_expression(node.args[0], aliases, owners)
                or (_is_owner(node.args[0], owners) and attribute is None)
            )
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "__getattribute__"
            and _is_owner(node.func.value, owners)
        ):
            attribute = _constant_string(node.args[0]) if node.args else None
            return attribute in {None, "store"}
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "__dict__"
            and _is_owner(node.func.value.value, owners)
        ):
            attribute = _constant_string(node.args[0]) if node.args else None
            return attribute in {None, "store"}
        if (
            isinstance(node.func, ast.Call)
            and _resolved_name(node.func.func, aliases) == "operator.attrgetter"
            and node.args
            and _is_owner(node.args[0], owners)
        ):
            attribute = _constant_string(node.func.args[0]) if node.func.args else None
            return attribute in {None, "store"}
    if isinstance(node, ast.Subscript):
        attribute = _constant_string(node.slice)
        if isinstance(node.value, ast.Attribute) and node.value.attr == "__dict__":
            return _is_owner(node.value.value, owners) and attribute in {None, "store"}
        if (
            isinstance(node.value, ast.Call)
            and _resolved_name(node.value.func, aliases) == "builtins.vars"
            and node.value.args
        ):
            return _is_owner(node.value.args[0], owners) and attribute in {None, "store"}
    return False


def declared_store_reads(root: Path) -> frozenset[str]:
    classes: dict[str, list[tuple[ast.ClassDef, str]]] = {}
    for source_path in sorted((root / "src/joiny_mnemonic").glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        relative = source_path.relative_to(root).as_posix()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, []).append((node, relative))
    memory_stores = classes.get("MemoryStore", [])
    if not memory_stores:
        raise ValueError("MemoryStore declaration not found")
    if len(memory_stores) != 1:
        raise ValueError("MemoryStore declaration is ambiguous")
    mro: dict[str, tuple[ast.ClassDef, str]] = {}
    pending = [memory_stores[0]]
    while pending:
        declaration = pending.pop()
        node, relative = declaration
        if node.name in mro:
            continue
        mro[node.name] = declaration
        for base in node.bases:
            base_name = _decorator_name(base)
            candidates = classes.get(str(base_name), [])
            if len(candidates) > 1:
                raise ValueError(f"store base class is ambiguous: {base_name}")
            if candidates:
                pending.append(candidates[0])
    declarations: dict[str, str] = {}
    for class_name in sorted(mro):
        node, relative = mro[class_name]
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
    aliases = _reflection_aliases(tree)
    owners = _owner_aliases(tree)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    writes: list[DirectStoreWrite] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.Subscript)) and _is_store_expression(
            node, aliases, owners
        ):
            writes.append(
                DirectStoreWrite(path, node.lineno, "<dynamic_store_access>")
            )
            continue
        if (
            isinstance(node, ast.Call)
            and _resolved_name(node.func, aliases) in {
                "builtins.delattr", "builtins.setattr",
            }
            and node.args
            and (
                _is_store_expression(node.args[0], aliases, owners)
                or (
                    _is_owner(node.args[0], owners)
                    and (
                        len(node.args) < 2
                        or _constant_string(node.args[1]) in {None, "store"}
                    )
                )
            )
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
