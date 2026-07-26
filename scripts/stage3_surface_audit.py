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
POSSIBILITY_SEPARATOR = "\x1f"


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


def _possibilities(value: str | None) -> frozenset[str]:
    return frozenset(value.split(POSSIBILITY_SEPARATOR)) if value else frozenset()


def _pack_possibilities(values: Iterable[str]) -> str:
    return POSSIBILITY_SEPARATOR.join(
        sorted(
            {
                option
                for value in values
                for option in _possibilities(value)
            }
        )
    )


def _resolved_names(node: ast.expr, aliases: dict[str, str]) -> frozenset[str]:
    if isinstance(node, ast.NamedExpr):
        return _resolved_names(node.value, aliases)
    if isinstance(node, ast.IfExp):
        return _resolved_names(node.body, aliases).union(
            _resolved_names(node.orelse, aliases)
        )
    if isinstance(node, ast.BoolOp):
        return frozenset().union(
            *(_resolved_names(value, aliases) for value in node.values)
        )
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return frozenset().union(
            *(_resolved_names(value, aliases) for value in node.elts)
        )
    if isinstance(node, ast.Dict):
        return frozenset().union(
            *(
                _resolved_names(value, aliases)
                for value in (*node.keys, *node.values)
                if value is not None
            )
        )
    name = _qualified_name(node)
    if name is None:
        return frozenset()
    if name in aliases:
        return _possibilities(aliases[name])
    head, separator, tail = name.partition(".")
    resolved_head = aliases.get(head)
    if separator and resolved_head:
        return frozenset(
            f"{option}.{tail}" for option in _possibilities(resolved_head)
        )
    return frozenset({name})


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


def _merge_possible_maps(
    mappings: Iterable[dict[str, str]],
) -> dict[str, str]:
    mappings = tuple(mappings)
    keys = {key for mapping in mappings for key in mapping}
    merged: dict[str, str] = {}
    for key in keys:
        values = {mapping[key] for mapping in mappings if key in mapping}
        merged[key] = _pack_possibilities(values)
    return merged


def _has_kind(value: str | None, *kinds: str) -> bool:
    return bool(_possibilities(value).intersection(kinds))


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
    if isinstance(node, ast.IfExp):
        kinds = {
            kind
            for value in (node.body, node.orelse)
            if (kind := _expression_kind(value, aliases, bindings)) is not None
        }
        return _pack_possibilities(kinds) if kinds else None
    if isinstance(node, ast.BoolOp):
        kinds = {
            kind
            for value in node.values
            if (kind := _expression_kind(value, aliases, bindings)) is not None
        }
        return _pack_possibilities(kinds) if kinds else None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        kinds = {
            kind
            for value in node.elts
            if (kind := _expression_kind(value, aliases, bindings)) is not None
        }
        return _pack_possibilities(kinds) if kinds else None
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        value_kind = _expression_kind(node.value, aliases, bindings)
        if node.attr == "service" and _has_kind(value_kind, "handler"):
            return "owner"
        if _has_kind(value_kind, "owner") and node.attr == "store":
            return "store"
        if _has_kind(value_kind, "owner") and node.attr == "__dict__":
            return "owner_dict"
        if _has_kind(value_kind, "handler") and node.attr == "__dict__":
            return "handler_dict"
        if _has_kind(value_kind, "owner") and node.attr == "__getattribute__":
            return "owner_getter"
        if _has_kind(value_kind, "store"):
            return "store_member"
    if isinstance(node, ast.Call):
        resolved = _resolved_names(node.func, aliases)
        if "builtins.getattr" in resolved and node.args:
            attribute = _constant_string(node.args[1]) if len(node.args) >= 2 else None
            target_kind = _expression_kind(node.args[0], aliases, bindings)
            if _has_kind(target_kind, "handler") and attribute == "service":
                return "owner"
            if _has_kind(target_kind, "handler") and attribute == "__dict__":
                return "handler_dict"
            if _has_kind(target_kind, "owner") and attribute == "__dict__":
                return "owner_dict"
            if _has_kind(target_kind, "owner") and attribute in {None, "store"}:
                return "store"
            if _has_kind(target_kind, "store"):
                return "store_member"
        if "object.__getattribute__" in resolved and node.args:
            attribute = _constant_string(node.args[1]) if len(node.args) >= 2 else None
            target_kind = _expression_kind(node.args[0], aliases, bindings)
            if _has_kind(target_kind, "handler") and attribute == "service":
                return "owner"
            if _has_kind(target_kind, "handler") and attribute == "__dict__":
                return "handler_dict"
            if _has_kind(target_kind, "owner") and attribute == "__dict__":
                return "owner_dict"
            if (
                _has_kind(
                    _expression_kind(node.args[0], aliases, bindings), "owner"
                )
                and attribute in {None, "store"}
            ):
                return "store"
        function_kind = _expression_kind(node.func, aliases, bindings)
        if _has_kind(function_kind, "owner_getter"):
            attribute = _constant_string(node.args[0]) if node.args else None
            if attribute in {None, "store"}:
                return "store"
        if _has_kind(function_kind, "store_getter") and node.args:
            if _has_kind(
                _expression_kind(node.args[0], aliases, bindings), "owner"
            ):
                return "store"
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop"}
        ):
            container_kind = _expression_kind(node.func.value, aliases, bindings)
            attribute = _constant_string(node.args[0]) if node.args else None
            if _has_kind(container_kind, "owner_dict") and attribute in {None, "store"}:
                return "store"
            if _has_kind(container_kind, "handler_dict") and attribute in {None, "service"}:
                return "owner"
        if "builtins.vars" in resolved and node.args:
            target_kind = _expression_kind(node.args[0], aliases, bindings)
            if _has_kind(target_kind, "owner"):
                return "owner_dict"
            if _has_kind(target_kind, "handler"):
                return "handler_dict"
        if "operator.attrgetter" in resolved:
            attribute = _constant_string(node.args[0]) if node.args else None
            if attribute in {None, "store"}:
                return "store_getter"
    if isinstance(node, ast.Subscript):
        attribute = _constant_string(node.slice)
        value_kind = _expression_kind(node.value, aliases, bindings)
        if _has_kind(value_kind, "handler_dict") and attribute == "service":
            return "owner"
        if (
            _has_kind(value_kind, "owner_dict")
            and attribute in {None, "store"}
        ):
            return "store"
    return None


def _analysis_context(
    nodes: Iterable[ast.AST], *,
    base_aliases: dict[str, str] | None = None,
    base_bindings: dict[str, str] | None = None,
    default_aliases: dict[str, str] | None = None,
    default_bindings: dict[str, str] | None = None,
    conditional_nodes: frozenset[ast.AST] = frozenset(),
) -> tuple[dict[str, str], dict[str, str]]:
    nodes = tuple(nodes)
    aliases = dict(REFLECTION_NAMES if base_aliases is None else base_aliases)
    for name in (node.arg for node in nodes if isinstance(node, ast.arg)):
        aliases.pop(name, None)
    aliases.update(default_aliases or {})
    bindings = dict(base_bindings or {})
    bindings["service"] = "owner"
    bindings["self"] = "handler"
    bindings.update(default_bindings or {})
    for node in nodes:
        prior_aliases = dict(aliases)
        prior_bindings = dict(bindings)
        binding_event = False
        if isinstance(node, ast.Import):
            binding_event = True
            for item in node.names:
                bound = item.asname or item.name.split(".")[0]
                aliases[bound] = (
                    item.name if item.asname else item.name.split(".")[0]
                )
                bindings.pop(bound, None)
        elif isinstance(node, ast.ImportFrom):
            binding_event = True
            for item in node.names:
                bound = item.asname or item.name
                if node.module in {"builtins", "operator"}:
                    aliases[bound] = f"{node.module}.{item.name}"
                else:
                    aliases.pop(bound, None)
                bindings.pop(bound, None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binding_event = True
            aliases.pop(node.name, None)
            bindings.pop(node.name, None)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            binding_event = True
            for assignment_target, assignment_value in _assignment_pairs((node,)):
                for target, value in _target_value_pairs(
                    assignment_target, assignment_value
                ):
                    if not isinstance(target, ast.Name):
                        continue
                    resolved = _resolved_names(value, aliases)
                    kind = _expression_kind(value, aliases, bindings)
                    transferable = resolved.intersection(TRANSFERABLE_CALLABLES)
                    if transferable:
                        aliases[target.id] = _pack_possibilities(transferable)
                    else:
                        aliases.pop(target.id, None)
                    if kind is not None:
                        bindings[target.id] = kind
                    elif target.id != "service":
                        bindings.pop(target.id, None)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            binding_event = True
            resolved = _resolved_names(node.iter, aliases)
            transferable = resolved.intersection(TRANSFERABLE_CALLABLES)
            kind = _expression_kind(node.iter, aliases, bindings)
            for name in _bound_target_names(node.target):
                if transferable:
                    aliases[name] = _pack_possibilities(transferable)
                else:
                    aliases.pop(name, None)
                if kind is not None:
                    bindings[name] = kind
                elif name != "service":
                    bindings.pop(name, None)
        elif isinstance(node, ast.AugAssign):
            binding_event = True
            for name in _bound_target_names(node.target):
                aliases.pop(name, None)
                if name != "service":
                    bindings.pop(name, None)
        elif isinstance(node, ast.Delete):
            binding_event = True
            for target in node.targets:
                for name in _bound_target_names(target):
                    aliases.pop(name, None)
                    if name != "service":
                        bindings.pop(name, None)
        if binding_event and node in conditional_nodes:
            aliases = _merge_possible_maps((prior_aliases, aliases))
            bindings = _merge_possible_maps((prior_bindings, bindings))
    return aliases, bindings


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
    conditional_nodes_by_scope: dict[ast.AST, frozenset[ast.AST]] = {}

    def position(node: ast.AST) -> tuple[int, int]:
        return (int(getattr(node, "lineno", 0)), int(getattr(node, "col_offset", 0)))

    def conditionally_executed(node: ast.AST, scope: ast.AST) -> bool:
        child = node
        parent = parents.get(child)
        while parent is not None and parent is not scope:
            if isinstance(parent, ast.If):
                if child is not parent.test:
                    return True
            elif isinstance(parent, ast.IfExp):
                if child is not parent.test:
                    return True
            elif isinstance(parent, (ast.For, ast.AsyncFor)):
                if child is not parent.iter:
                    return True
            elif isinstance(parent, ast.While):
                if child is not parent.test:
                    return True
            elif isinstance(parent, ast.BoolOp):
                if child is not parent.values[0]:
                    return True
            elif isinstance(
                parent,
                (
                    ast.Try,
                    ast.TryStar,
                    ast.With,
                    ast.AsyncWith,
                    ast.Match,
                    ast.comprehension,
                ),
            ):
                return True
            child = parent
            parent = parents.get(child)
        return False

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
        return _analysis_context(
            prefix,
            base_aliases=base_aliases,
            base_bindings=base_bindings,
            default_aliases=default_aliases,
            default_bindings=default_bindings,
            conditional_nodes=conditional_nodes_by_scope[scope],
        )

    def expression_can_be_closure(
        node: ast.expr, aliases: set[str], scope: ast.AST
    ) -> bool:
        if node is scope:
            return True
        if isinstance(node, ast.Name):
            return node.id in aliases
        if isinstance(node, ast.NamedExpr):
            return expression_can_be_closure(node.value, aliases, scope)
        if isinstance(node, ast.IfExp):
            return expression_can_be_closure(
                node.body, aliases, scope
            ) or expression_can_be_closure(node.orelse, aliases, scope)
        if isinstance(node, ast.BoolOp):
            return any(
                expression_can_be_closure(value, aliases, scope)
                for value in node.values
            )
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(
                expression_can_be_closure(value, aliases, scope)
                for value in node.elts
            )
        if isinstance(node, ast.Dict):
            return any(
                expression_can_be_closure(value, aliases, scope)
                for value in (*node.keys, *node.values)
                if value is not None
            )
        return False

    def subtree_references_closure(
        node: ast.AST, aliases: set[str], scope: ast.AST
    ) -> bool:
        return any(
            child is scope
            or (
                isinstance(child, ast.Name)
                and isinstance(child.ctx, ast.Load)
                and child.id in aliases
            )
            for child in ast.walk(node)
        )

    def closure_runtime_contexts(
        parent: ast.AST, scope: ast.AST
    ) -> list[tuple[dict[str, str], dict[str, str]]]:
        contexts = [context_at(parent)]
        closure_aliases: set[str] = set()
        for candidate in scope_data[parent][0]:
            prior_aliases = set(closure_aliases)
            escaped = False
            if candidate is scope and isinstance(
                scope, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                closure_aliases.add(scope.name)
            elif isinstance(candidate, ast.Import):
                for item in candidate.names:
                    closure_aliases.discard(
                        item.asname or item.name.split(".")[0]
                    )
            elif isinstance(candidate, ast.ImportFrom):
                for item in candidate.names:
                    closure_aliases.discard(item.asname or item.name)
            elif isinstance(
                candidate, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                closure_aliases.discard(candidate.name)
            elif isinstance(candidate, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                for target, value in _assignment_pairs((candidate,)):
                    possible = expression_can_be_closure(
                        value, closure_aliases, scope
                    )
                    target_names = _bound_target_names(target)
                    if possible and not target_names:
                        escaped = True
                    for name in target_names:
                        if possible:
                            closure_aliases.add(name)
                        else:
                            closure_aliases.discard(name)
            elif isinstance(
                candidate, (ast.For, ast.AsyncFor, ast.comprehension)
            ):
                possible = expression_can_be_closure(
                    candidate.iter, closure_aliases, scope
                )
                for name in _bound_target_names(candidate.target):
                    if possible:
                        closure_aliases.add(name)
                    else:
                        closure_aliases.discard(name)
            elif isinstance(candidate, ast.AugAssign):
                closure_aliases.difference_update(
                    _bound_target_names(candidate.target)
                )
            elif isinstance(candidate, ast.Delete):
                for target in candidate.targets:
                    closure_aliases.difference_update(_bound_target_names(target))
            if candidate in conditional_nodes_by_scope[parent]:
                closure_aliases.update(prior_aliases)
            if isinstance(candidate, ast.Call):
                direct_call = candidate.func is scope or (
                    isinstance(candidate.func, ast.Name)
                    and candidate.func.id in closure_aliases
                )
                argument_escape = any(
                    subtree_references_closure(argument, closure_aliases, scope)
                    for argument in (
                        *candidate.args,
                        *(keyword.value for keyword in candidate.keywords),
                    )
                )
                if direct_call or argument_escape:
                    contexts.append(context_at(parent, candidate))
            elif isinstance(candidate, (ast.Return, ast.Yield, ast.YieldFrom)):
                value = candidate.value
                if value is not None and subtree_references_closure(
                    value, closure_aliases, scope
                ):
                    contexts.append(context_at(parent, candidate))
            if escaped:
                contexts.append(context_at(parent, candidate))
        return contexts

    for scope in scopes:
        nodes = _scope_nodes(scope)
        conditional_nodes_by_scope[scope] = frozenset(
            node for node in nodes if conditionally_executed(node, scope)
        )
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
            runtime_contexts = closure_runtime_contexts(parent, scope)
            parent_aliases = _merge_possible_maps(
                aliases for aliases, _ in runtime_contexts
            )
            parent_bindings = _merge_possible_maps(
                bindings for _, bindings in runtime_contexts
            )
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
                    resolved = _resolved_names(default, definition_aliases)
                    transferable = resolved.intersection(TRANSFERABLE_CALLABLES)
                    if transferable:
                        default_aliases[argument.arg] = _pack_possibilities(
                            transferable
                        )
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
            if (
                isinstance(node, ast.Call)
                and _resolved_names(node.func, aliases).intersection(
                    {"builtins.eval", "builtins.exec"}
                )
            ):
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
                and _has_kind(
                    _expression_kind(node, aliases, bindings),
                    "owner_dict",
                    "owner_getter",
                    "store_getter",
                )
            ):
                writes.append(
                    DirectStoreWrite(
                        path, node.lineno, "<raw_store_capability_escape>"
                    )
                )
                continue
            if (
                isinstance(node, (ast.Call, ast.Subscript))
                and _has_kind(
                    _expression_kind(node, aliases, bindings),
                    "store",
                    "store_member",
                )
            ):
                writes.append(
                    DirectStoreWrite(path, node.lineno, "<dynamic_store_access>")
                )
                continue
            if (
                isinstance(node, ast.Call)
                and _resolved_names(node.func, aliases).intersection(
                    {"builtins.delattr", "builtins.setattr"}
                )
                and node.args
                and (
                    _has_kind(
                        _expression_kind(node.args[0], aliases, bindings),
                        "store",
                        "store_member",
                    )
                    or (
                        _has_kind(
                            _expression_kind(node.args[0], aliases, bindings),
                            "owner",
                        )
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
                or not _has_kind(
                    _expression_kind(node.value, aliases, bindings), "owner"
                )
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
