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
    "object.__getattribute__": "object.__getattribute__",
}
TRANSFERABLE_CALLABLES = frozenset({
    *REFLECTION_NAMES.values(),
    "operator.attrgetter",
})


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
    if isinstance(node, ast.NamedExpr):
        return _resolved_name(node.value, aliases)
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

    def visit(parent: ast.AST) -> None:
        for node in ast.iter_child_nodes(parent):
            nodes.append(node)
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                continue
            visit(node)

    visit(root)
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


def _bound_target_names(target: ast.expr) -> frozenset[str]:
    if isinstance(target, ast.Name):
        return frozenset({target.id})
    if isinstance(target, (ast.Tuple, ast.List)):
        return frozenset(
            name for item in target.elts for name in _bound_target_names(item)
        )
    return frozenset()


def _reflection_aliases(
    nodes: Iterable[ast.AST], *, base: dict[str, str] | None = None,
    defaults: dict[str, str] | None = None,
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
    aliases.update(defaults or {})
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
                if resolved in TRANSFERABLE_CALLABLES:
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
    if isinstance(node, ast.NamedExpr):
        return _expression_kind(node.value, aliases, bindings)
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        value_kind = _expression_kind(node.value, aliases, bindings)
        if node.attr == "service" and value_kind == "handler":
            return "owner"
        if value_kind == "owner" and node.attr == "store":
            return "store"
        if value_kind == "owner" and node.attr == "__dict__":
            return "owner_dict"
        if value_kind == "handler" and node.attr == "__dict__":
            return "handler_dict"
        if value_kind == "owner" and node.attr == "__getattribute__":
            return "owner_getter"
        if value_kind == "store":
            return "store_member"
    if isinstance(node, ast.Call):
        resolved = _resolved_name(node.func, aliases)
        if resolved == "builtins.getattr" and node.args:
            attribute = _constant_string(node.args[1]) if len(node.args) >= 2 else None
            target_kind = _expression_kind(node.args[0], aliases, bindings)
            if target_kind == "handler" and attribute == "service":
                return "owner"
            if target_kind == "handler" and attribute == "__dict__":
                return "handler_dict"
            if target_kind == "owner" and attribute == "__dict__":
                return "owner_dict"
            if target_kind == "owner" and attribute in {None, "store"}:
                return "store"
            if target_kind == "store":
                return "store_member"
        if resolved == "object.__getattribute__" and node.args:
            attribute = _constant_string(node.args[1]) if len(node.args) >= 2 else None
            target_kind = _expression_kind(node.args[0], aliases, bindings)
            if target_kind == "handler" and attribute == "service":
                return "owner"
            if target_kind == "handler" and attribute == "__dict__":
                return "handler_dict"
            if target_kind == "owner" and attribute == "__dict__":
                return "owner_dict"
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
        ):
            container_kind = _expression_kind(node.func.value, aliases, bindings)
            attribute = _constant_string(node.args[0]) if node.args else None
            if container_kind == "owner_dict" and attribute in {None, "store"}:
                return "store"
            if container_kind == "handler_dict" and attribute in {None, "service"}:
                return "owner"
        if resolved == "builtins.vars" and node.args:
            target_kind = _expression_kind(node.args[0], aliases, bindings)
            if target_kind == "owner":
                return "owner_dict"
            if target_kind == "handler":
                return "handler_dict"
        if resolved == "operator.attrgetter":
            attribute = _constant_string(node.args[0]) if node.args else None
            if attribute in {None, "store"}:
                return "store_getter"
    if isinstance(node, ast.Subscript):
        attribute = _constant_string(node.slice)
        value_kind = _expression_kind(node.value, aliases, bindings)
        if value_kind == "handler_dict" and attribute == "service":
            return "owner"
        if (
            value_kind == "owner_dict"
            and attribute in {None, "store"}
        ):
            return "store"
    return None


def _value_bindings(
    nodes: Iterable[ast.AST], aliases: dict[str, str],
    *, base: dict[str, str] | None = None,
) -> dict[str, str]:
    nodes = tuple(nodes)
    bindings = dict(base or {})
    bindings["service"] = "owner"
    bindings["self"] = "handler"
    assignments = _assignment_pairs(nodes)
    for _ in range(len(assignments) + 1):
        changed = False
        for assignment_target, assignment_value in assignments:
            for target, value in _target_value_pairs(assignment_target, assignment_value):
                kind = _expression_kind(value, aliases, bindings)
                if not isinstance(target, ast.Name):
                    continue
                if kind is not None:
                    if bindings.get(target.id) != kind:
                        bindings[target.id] = kind
                        changed = True
                elif target.id in bindings and target.id != "service":
                    bindings.pop(target.id)
                    changed = True
        if not changed:
            break
    return bindings


def declared_store_reads(root: Path) -> frozenset[str]:
    classes: dict[tuple[str, str], list[tuple[ast.ClassDef, str]]] = {}
    imports: dict[str, dict[str, tuple[str, str | None]]] = {}
    imports_at_class: dict[ast.ClassDef, dict[str, tuple[str, str | None]]] = {}
    last_bindings: dict[tuple[str, str], ast.AST] = {}
    for source_path in sorted((root / "src/joiny_mnemonic").glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        relative = source_path.relative_to(root).as_posix()
        module = source_path.stem
        module_imports: dict[str, tuple[str, str | None]] = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.setdefault((module, node.name), []).append((node, relative))
                imports_at_class[node] = dict(module_imports)
                last_bindings[(module, node.name)] = node
                module_imports.pop(node.name, None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                last_bindings[(module, node.name)] = node
                module_imports.pop(node.name, None)
            elif isinstance(node, ast.ImportFrom):
                imported_module = node.module
                if node.level == 1 and imported_module:
                    imported_module = f"joiny_mnemonic.{imported_module}"
                elif node.level >= 2:
                    imported_module = f"__foreign_relative_{node.level}__.{node.module or ''}"
                for item in node.names:
                    local_name = item.asname or item.name
                    last_bindings[(module, local_name)] = node
                    if imported_module:
                        module_imports[local_name] = (
                            imported_module, item.name,
                        )
                    else:
                        module_imports[local_name] = (
                            f"joiny_mnemonic.{item.name}", None,
                        )
            elif isinstance(node, ast.Import):
                for item in node.names:
                    local_name = item.asname or item.name.split(".")[0]
                    last_bindings[(module, local_name)] = node
                    module_imports[local_name] = (
                        item.name, None,
                    )
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for name in _bound_target_names(target):
                        last_bindings[(module, name)] = node
                        module_imports.pop(name, None)
            elif isinstance(node, ast.AnnAssign):
                for name in _bound_target_names(node.target):
                    last_bindings[(module, name)] = node
                    module_imports.pop(name, None)
            elif isinstance(node, (ast.AugAssign, ast.Delete)):
                targets = [node.target] if isinstance(node, ast.AugAssign) else node.targets
                for target in targets:
                    for name in _bound_target_names(target):
                        last_bindings[(module, name)] = node
                        module_imports.pop(name, None)
        imports[module] = module_imports
    memory_key = ("storage", "MemoryStore")
    memory_stores = classes.get(memory_key, [])
    memory_binding = last_bindings.get(memory_key)
    if not memory_stores or not isinstance(memory_binding, ast.ClassDef):
        return frozenset()
    if memory_binding is not memory_stores[-1][0]:
        raise ValueError("MemoryStore declaration not found")
    unresolved_base = False

    def declaration_for(
        key: tuple[str, str]
    ) -> tuple[ast.ClassDef, str] | None:
        binding = last_bindings.get(key)
        candidates = classes.get(key, [])
        if not isinstance(binding, ast.ClassDef):
            return None
        return next((item for item in reversed(candidates) if item[0] is binding), None)

    def local_bases(
        key: tuple[str, str], declaration: tuple[ast.ClassDef, str]
    ) -> tuple[tuple[str, str], ...]:
        nonlocal unresolved_base
        node, relative = declaration
        module, _ = key
        class_imports = imports_at_class[node]
        resolved_bases: list[tuple[str, str]] = []
        for base in node.bases:
            base_key: tuple[str, str] | None = None
            if isinstance(base, ast.Name):
                if base.id == "object":
                    continue
                imported = class_imports.get(base.id)
                if imported and imported[1]:
                    imported_module = imported[0]
                    if (
                        imported_module.startswith("joiny_mnemonic.")
                        and imported_module.count(".") == 1
                    ):
                        base_key = (
                            imported_module.rsplit(".", 1)[-1], str(imported[1])
                        )
                else:
                    base_key = (module, base.id)
            elif isinstance(base, ast.Attribute):
                qualified = _qualified_name(base)
                parts = qualified.split(".") if qualified else []
                if len(parts) >= 2:
                    imported = class_imports.get(parts[0])
                    if imported and imported[1] is None:
                        imported_module = imported[0]
                        if (
                            imported_module.startswith("joiny_mnemonic.")
                            and imported_module.count(".") == 1
                        ):
                            base_key = (
                                imported_module.rsplit(".", 1)[-1], parts[-1]
                            )
                    elif parts[0] == "joiny_mnemonic" and len(parts) == 3:
                        base_key = (parts[-2], parts[-1])
            if base_key is None:
                unresolved_base = True
                continue
            if declaration_for(base_key) is not None:
                resolved_bases.append(base_key)
            else:
                unresolved_base = True
        return tuple(resolved_bases)

    mro_cache: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
    resolving: set[tuple[str, str]] = set()

    def linearize(key: tuple[str, str]) -> tuple[tuple[str, str], ...]:
        if key in mro_cache:
            return mro_cache[key]
        if key in resolving:
            raise ValueError(f"cyclic store inheritance: {key[0]}.{key[1]}")
        resolving.add(key)
        declaration = declaration_for(key)
        if declaration is None:
            raise ValueError(f"unresolved store class: {key[0]}.{key[1]}")
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

    def class_local_bound_before(
        class_node: ast.ClassDef, member: ast.AST, name: str
    ) -> bool:
        bound: set[str] = set()
        for statement in class_node.body:
            if statement is member:
                break
            if isinstance(statement, ast.Delete):
                for target in statement.targets:
                    bound.difference_update(_bound_target_names(target))
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(statement.name)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    bound.update(_bound_target_names(target))
            elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
                bound.update(_bound_target_names(statement.target))
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                for item in statement.names:
                    bound.add(item.asname or item.name.split(".")[0])
        return name in bound

    for class_key in mro_order:
        if unresolved_base and class_key != memory_key:
            break
        declaration = declaration_for(class_key)
        if declaration is None:
            break
        node, relative = declaration
        module, _ = class_key
        class_imports = imports_at_class[node]
        deleted: set[str] = set()
        for member in reversed(node.body):
            if isinstance(member, ast.Delete):
                for target in member.targets:
                    deleted.update(_bound_target_names(target))
                continue
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound_names = frozenset({member.name})
            elif isinstance(member, ast.Assign):
                bound_names = frozenset(
                    name
                    for target in member.targets
                    for name in _bound_target_names(target)
                )
            elif isinstance(member, ast.AnnAssign):
                bound_names = _bound_target_names(member.target)
            elif isinstance(member, ast.AugAssign):
                bound_names = _bound_target_names(member.target)
            else:
                bound_names = frozenset()
            new_names = bound_names - effective - deleted
            effective.update(new_names)
            if (
                not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                or member.name not in new_names
            ):
                continue
            canonical = False
            for decorator in member.decorator_list:
                if isinstance(decorator, ast.Name):
                    canonical = (
                        not class_local_bound_before(node, member, decorator.id)
                        and class_imports.get(decorator.id) == (
                        "joiny_mnemonic.storage_support", "store_read",
                        )
                    )
                elif isinstance(decorator, ast.Attribute) and isinstance(
                    decorator.value, ast.Name
                ):
                    canonical = (
                        class_imports.get(decorator.value.id)
                        == ("joiny_mnemonic.storage_support", None)
                        and decorator.attr == "store_read"
                    )
                if canonical:
                    declarations.add(member.name)
                    break
    return frozenset(declarations)


def calls_in_source(
    source: str, *, path: str, read_methods: frozenset[str] = frozenset(),
    _base_aliases: dict[str, str] | None = None,
    _base_bindings: dict[str, str] | None = None,
) -> tuple[DirectStoreWrite, ...]:
    tree = ast.parse(source, filename=path)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    writes: list[DirectStoreWrite] = []
    scopes: list[ast.AST] = [tree]
    scopes.extend(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    )
    scope_data: dict[
        ast.AST,
        tuple[
            tuple[ast.AST, ...], dict[str, str], dict[str, str],
            dict[str, str], dict[str, str],
        ],
    ] = {}

    def position(node: ast.AST) -> tuple[int, int]:
        return (int(getattr(node, "lineno", 0)), int(getattr(node, "col_offset", 0)))

    def context_at(
        scope: ast.AST, target: ast.AST | None = None
    ) -> tuple[dict[str, str], dict[str, str]]:
        nodes, base_aliases, base_bindings, default_aliases, default_bindings = (
            scope_data[scope]
        )
        if target is None:
            prefix = nodes
        else:
            target_position = position(target)
            prefix = tuple(
                node
                for node in nodes
                if not hasattr(node, "lineno") or position(node) <= target_position
            )
        aliases = _reflection_aliases(
            prefix, base=base_aliases, defaults=default_aliases
        )
        bindings = _value_bindings(
            prefix,
            aliases,
            base={**base_bindings, **default_bindings},
        )
        return aliases, bindings

    for scope in scopes:
        nodes = _scope_nodes(scope)
        if scope is tree:
            scope_data[tree] = (
                nodes,
                dict(REFLECTION_NAMES if _base_aliases is None else _base_aliases),
                dict(_base_bindings or {}),
                {},
                {},
            )
        else:
            parent = parents.get(scope)
            while parent not in scope_data:
                parent = parents.get(parent)
            parent_aliases, parent_bindings = context_at(parent)
            definition_aliases, definition_bindings = context_at(parent, scope)
            default_aliases: dict[str, str] = {}
            default_bindings: dict[str, str] = {}
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                positional = [*scope.args.posonlyargs, *scope.args.args]
                defaults = [
                    *zip(positional[-len(scope.args.defaults):], scope.args.defaults)
                ] if scope.args.defaults else []
                defaults.extend(
                    (argument, default)
                    for argument, default in zip(
                        scope.args.kwonlyargs, scope.args.kw_defaults
                    )
                    if default is not None
                )
                for argument, default in defaults:
                    resolved = _resolved_name(default, definition_aliases)
                    if resolved in TRANSFERABLE_CALLABLES:
                        default_aliases[argument.arg] = resolved
                    kind = _expression_kind(
                        default, definition_aliases, definition_bindings
                    )
                    if kind is not None:
                        default_bindings[argument.arg] = kind
            scope_data[scope] = (
                nodes,
                parent_aliases,
                parent_bindings,
                default_aliases,
                default_bindings,
            )
        for node in nodes:
            aliases, bindings = context_at(scope, node)
            if isinstance(node, ast.Call) and _resolved_name(node.func, aliases) in {
                "builtins.eval", "builtins.exec",
            }:
                literal = _literal_code(node.args[0]) if node.args else None
                if literal is not None and calls_in_source(
                    literal,
                    path=path,
                    read_methods=read_methods,
                    _base_aliases=aliases,
                    _base_bindings=bindings,
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
