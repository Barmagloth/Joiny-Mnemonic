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
    "eval": "builtins.eval",
    "exec": "builtins.exec",
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


def _scope_nodes(root: ast.AST) -> tuple[ast.AST, ...]:
    nodes: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        nodes.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return tuple(nodes)


def _assignment_pairs(nodes: Iterable[ast.AST]) -> tuple[tuple[ast.expr, ast.expr], ...]:
    pairs: list[tuple[ast.expr, ast.expr]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            pairs.extend((target, node.value) for target in node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs.append((node.target, node.value))
        elif isinstance(node, ast.NamedExpr):
            pairs.append((node.target, node.value))
    return tuple(pairs)


def _target_value_pairs(target: ast.expr, value: ast.expr) -> tuple[tuple[ast.expr, ast.expr], ...]:
    if (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        pairs: list[tuple[ast.expr, ast.expr]] = []
        for target_item, value_item in zip(target.elts, value.elts):
            pairs.extend(_target_value_pairs(target_item, value_item))
        return tuple(pairs)
    return ((target, value),)


def _reflection_aliases(
    nodes: Iterable[ast.AST], *, base: dict[str, str] | None = None
) -> dict[str, str]:
    nodes = tuple(nodes)
    aliases = dict(REFLECTION_NAMES if base is None else base)
    shadowed = {
        node.name
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    shadowed.update(
        node.arg for node in nodes if isinstance(node, ast.arg)
    )
    for name in shadowed:
        aliases.pop(name, None)
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"builtins", "operator"}:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    assignments = _assignment_pairs(nodes)
    for _ in range(len(assignments) + 1):
        changed = False
        for assignment_target, assignment_value in assignments:
            for target, value in _target_value_pairs(assignment_target, assignment_value):
                if not isinstance(target, ast.Name):
                    continue
                resolved = _resolved_name(value, aliases)
                if resolved in {
                "builtins.delattr", "builtins.eval", "builtins.exec",
                "builtins.getattr", "builtins.setattr", "builtins.vars",
                "operator.attrgetter",
                }:
                    if aliases.get(target.id) != resolved:
                        aliases[target.id] = resolved
                        changed = True
                elif target.id in aliases:
                    aliases.pop(target.id)
                    changed = True
        if not changed:
            break
    return aliases


def _constant_string(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_code(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        try:
            return node.value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_code(node.left)
        right = _literal_code(node.right)
        return left + right if left is not None and right is not None else None
    return None


def _expression_kind(
    node: ast.expr, aliases: dict[str, str], bindings: dict[str, str]
) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        if node.attr == "service":
            return "owner"
        value_kind = _expression_kind(node.value, aliases, bindings)
        if value_kind == "owner" and node.attr == "store":
            return "store"
        if value_kind == "owner" and node.attr == "__dict__":
            return "owner_dict"
        if value_kind == "owner" and node.attr == "__getattribute__":
            return "owner_getter"
        if value_kind == "store":
            return "store_member"
    if isinstance(node, ast.Call):
        resolved = _resolved_name(node.func, aliases)
        if resolved == "builtins.getattr" and node.args:
            attribute = _constant_string(node.args[1]) if len(node.args) >= 2 else None
            target_kind = _expression_kind(node.args[0], aliases, bindings)
            if target_kind == "owner" and attribute in {None, "store"}:
                return "store"
            if target_kind == "store":
                return "store_member"
        if resolved == "object.__getattribute__" and node.args:
            attribute = _constant_string(node.args[1]) if len(node.args) >= 2 else None
            if (
                _expression_kind(node.args[0], aliases, bindings) == "owner"
                and attribute in {None, "store"}
            ):
                return "store"
        function_kind = _expression_kind(node.func, aliases, bindings)
        if function_kind == "owner_getter":
            attribute = _constant_string(node.args[0]) if node.args else None
            if attribute in {None, "store"}:
                return "store"
        if function_kind == "store_getter" and node.args:
            if _expression_kind(node.args[0], aliases, bindings) == "owner":
                return "store"
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop"}
            and _expression_kind(node.func.value, aliases, bindings) == "owner_dict"
        ):
            attribute = _constant_string(node.args[0]) if node.args else None
            if attribute in {None, "store"}:
                return "store"
        if resolved == "builtins.vars" and node.args:
            if _expression_kind(node.args[0], aliases, bindings) == "owner":
                return "owner_dict"
        if resolved == "operator.attrgetter":
            attribute = _constant_string(node.args[0]) if node.args else None
            if attribute in {None, "store"}:
                return "store_getter"
    if isinstance(node, ast.Subscript):
        attribute = _constant_string(node.slice)
        if (
            _expression_kind(node.value, aliases, bindings) == "owner_dict"
            and attribute in {None, "store"}
        ):
            return "store"
    return None


def _value_bindings(nodes: Iterable[ast.AST], aliases: dict[str, str]) -> dict[str, str]:
    nodes = tuple(nodes)
    bindings = {"service": "owner"}
    assignments = _assignment_pairs(nodes)
    for _ in range(len(assignments) + 1):
        changed = False
        for assignment_target, assignment_value in assignments:
            for target, value in _target_value_pairs(assignment_target, assignment_value):
                kind = _expression_kind(value, aliases, bindings)
                if kind is not None and isinstance(target, ast.Name):
                    if bindings.get(target.id) != kind:
                        bindings[target.id] = kind
                        changed = True
        if not changed:
            break
    return bindings


def declared_store_reads(root: Path) -> frozenset[str]:
    classes: dict[tuple[str, str], list[tuple[ast.ClassDef, str]]] = {}
    imports: dict[str, dict[str, tuple[str, str | None]]] = {}
    for source_path in sorted((root / "src/joiny_mnemonic").glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        relative = source_path.relative_to(root).as_posix()
        module = source_path.stem
        module_imports: dict[str, tuple[str, str | None]] = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.setdefault((module, node.name), []).append((node, relative))
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_module = node.module.split(".")[-1]
                for item in node.names:
                    module_imports[item.asname or item.name] = (
                        imported_module, item.name,
                    )
            elif isinstance(node, ast.Import):
                for item in node.names:
                    imported_module = item.name.split(".")[-1]
                    module_imports[item.asname or imported_module] = (
                        imported_module, None,
                    )
        imports[module] = module_imports
    memory_keys = [key for key in classes if key[1] == "MemoryStore"]
    memory_stores = [item for key in memory_keys for item in classes[key]]
    if not memory_stores:
        raise ValueError("MemoryStore declaration not found")
    if len(memory_stores) != 1:
        raise ValueError("MemoryStore declaration is ambiguous")
    memory_key = memory_keys[0]
    def local_bases(
        key: tuple[str, str], declaration: tuple[ast.ClassDef, str]
    ) -> tuple[tuple[str, str], ...]:
        node, relative = declaration
        module, _ = key
        resolved_bases: list[tuple[str, str]] = []
        for base in node.bases:
            base_key: tuple[str, str] | None = None
            if isinstance(base, ast.Name):
                imported = imports[module].get(base.id)
                if imported and imported[1]:
                    base_key = (imported[0], str(imported[1]))
                else:
                    base_key = (module, base.id)
            elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                imported = imports[module].get(base.value.id)
                if imported and imported[1] is None:
                    base_key = (imported[0], base.attr)
            if base_key is None:
                continue
            candidates = classes.get(base_key, [])
            if len(candidates) > 1:
                raise ValueError(
                    f"store base class is ambiguous: {base_key[0]}.{base_key[1]}"
                )
            if candidates:
                resolved_bases.append(base_key)
        return tuple(resolved_bases)

    mro_cache: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
    resolving: set[tuple[str, str]] = set()

    def linearize(key: tuple[str, str]) -> tuple[tuple[str, str], ...]:
        if key in mro_cache:
            return mro_cache[key]
        if key in resolving:
            raise ValueError(f"cyclic store inheritance: {key[0]}.{key[1]}")
        resolving.add(key)
        declaration = classes[key][0]
        bases = local_bases(key, declaration)
        sequences = [list(linearize(base)) for base in bases]
        sequences.append(list(bases))
        merged: list[tuple[str, str]] = []
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            candidate = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if all(sequence[0] not in other[1:] for other in sequences)
                ),
                None,
            )
            if candidate is None:
                raise ValueError(f"inconsistent store MRO: {key[0]}.{key[1]}")
            merged.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] == candidate:
                    sequence.pop(0)
        resolving.remove(key)
        result = (key, *merged)
        mro_cache[key] = result
        return result

    mro_order = linearize(memory_key)
    effective: set[str] = set()
    declarations: set[str] = set()
    for class_key in mro_order:
        node, relative = classes[class_key][0]
        module, _ = class_key
        for member in node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if member.name in effective:
                continue
            effective.add(member.name)
            canonical = False
            for decorator in member.decorator_list:
                if isinstance(decorator, ast.Name):
                    canonical = imports[module].get(decorator.id) == (
                        "storage_support", "store_read",
                    )
                elif isinstance(decorator, ast.Attribute) and isinstance(
                    decorator.value, ast.Name
                ):
                    canonical = (
                        imports[module].get(decorator.value.id)
                        == ("storage_support", None)
                        and decorator.attr == "store_read"
                    )
                if canonical:
                    declarations.add(member.name)
                    break
    return frozenset(declarations)


def calls_in_source(
    source: str, *, path: str, read_methods: frozenset[str] = frozenset(),
    _depth: int = 0,
) -> tuple[DirectStoreWrite, ...]:
    tree = ast.parse(source, filename=path)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    writes: list[DirectStoreWrite] = []
    module_nodes = _scope_nodes(tree)
    module_aliases = _reflection_aliases(module_nodes)
    scopes: list[ast.AST] = [tree]
    scopes.extend(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    )
    for scope in scopes:
        nodes = _scope_nodes(scope)
        aliases = (
            module_aliases
            if scope is tree
            else _reflection_aliases(nodes, base=module_aliases)
        )
        bindings = _value_bindings(nodes, aliases)
        for node in nodes:
            if isinstance(node, ast.Call) and _resolved_name(node.func, aliases) in {
                "builtins.eval", "builtins.exec",
            }:
                literal = _literal_code(node.args[0]) if node.args else None
                if literal is not None and _depth < 3 and calls_in_source(
                    literal, path=path, read_methods=read_methods, _depth=_depth + 1
                ):
                    writes.append(
                        DirectStoreWrite(
                            path, node.lineno, "<literal_dynamic_store_access>"
                        )
                    )
                continue
            if (
                isinstance(node, (ast.Call, ast.Subscript, ast.Attribute))
                and _expression_kind(node, aliases, bindings) in {
                    "owner_dict", "owner_getter", "store_getter",
                }
            ):
                writes.append(
                    DirectStoreWrite(
                        path, node.lineno, "<raw_store_capability_escape>"
                    )
                )
                continue
            if (
                isinstance(node, (ast.Call, ast.Subscript))
                and _expression_kind(node, aliases, bindings)
                in {"store", "store_member"}
            ):
                writes.append(
                    DirectStoreWrite(path, node.lineno, "<dynamic_store_access>")
                )
                continue
            if (
                isinstance(node, ast.Call)
                and _resolved_name(node.func, aliases)
                in {"builtins.delattr", "builtins.setattr"}
                and node.args
                and (
                    _expression_kind(node.args[0], aliases, bindings)
                    in {"store", "store_member"}
                    or (
                        _expression_kind(node.args[0], aliases, bindings) == "owner"
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
            if (
                not isinstance(node, ast.Attribute)
                or node.attr != "store"
                or _expression_kind(node.value, aliases, bindings) != "owner"
            ):
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
