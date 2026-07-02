from __future__ import annotations

import ast
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class CodeSymbol:
    id: str
    name: str
    qualified_name: str
    kind: str
    module: str
    path: str
    line: int
    end_line: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class CodeEdge:
    source: str
    target: str
    kind: str
    resolved: bool
    line: int


@dataclass(frozen=True, slots=True)
class CodeGraphReport:
    language: str
    files: int
    symbols: int
    edges: int
    unresolved_calls: int
    parse_errors: tuple[dict[str, str], ...]


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, module: str, path: str, content_hash: str) -> None:
        self.module = module
        self.path = path
        self.content_hash = content_hash
        self.scope: list[str] = []
        self.symbol_stack: list[str] = []
        self.symbols: list[CodeSymbol] = []
        self.calls: list[tuple[str, str, int]] = []
        self.imports: list[tuple[str, int]] = []

    def _symbol(self, node: ast.AST, name: str, kind: str) -> str:
        qualname = ".".join([*self.scope, name])
        symbol_id = f"{self.module}:{qualname}"
        self.symbols.append(
            CodeSymbol(
                id=symbol_id,
                name=name,
                qualified_name=qualname,
                kind=kind,
                module=self.module,
                path=self.path,
                line=int(getattr(node, "lineno", 1)),
                end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
                content_hash=self.content_hash,
            )
        )
        return symbol_id

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        symbol_id = self._symbol(node, node.name, "class")
        self.scope.append(node.name)
        self.symbol_stack.append(symbol_id)
        self.generic_visit(node)
        self.symbol_stack.pop()
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = "method" if self.scope and any(
            symbol.kind == "class" and symbol.qualified_name == ".".join(self.scope)
            for symbol in self.symbols
        ) else "function"
        symbol_id = self._symbol(node, node.name, kind)
        self.scope.append(node.name)
        self.symbol_stack.append(symbol_id)
        self.generic_visit(node)
        self.symbol_stack.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    @staticmethod
    def _call_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parts: list[str] = []
            current: ast.AST = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts)) if parts else None
        return None

    def visit_Call(self, node: ast.Call) -> Any:
        if self.symbol_stack:
            name = self._call_name(node.func)
            if name:
                self.calls.append((self.symbol_stack[-1], name, int(node.lineno)))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> Any:
        self.imports.extend((alias.name, int(node.lineno)) for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        prefix = "." * node.level + (node.module or "")
        self.imports.append((prefix, int(node.lineno)))


class PythonCodeIndex:
    """Live Python AST symbol/call graph with reverse-impact traversal."""

    DEFAULT_EXCLUDES = {
        ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
        ".joiny-mnemonic", ".llm-memory", ".pytest_cache", "dist", "build",
    }

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.symbols: dict[str, CodeSymbol] = {}
        self.edges: list[CodeEdge] = []
        self.parse_errors: list[dict[str, str]] = []
        self.file_count = 0
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()

    def _files(self) -> list[Path]:
        return sorted(
            path for path in self.project_root.rglob("*.py")
            if not any(part in self.DEFAULT_EXCLUDES for part in path.relative_to(self.project_root).parts)
        )

    def _current_fingerprint(self, files: Iterable[Path]) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            (path.relative_to(self.project_root).as_posix(), path.stat().st_mtime_ns, path.stat().st_size)
            for path in files
        )

    @staticmethod
    def _module(relative: Path) -> str:
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts) or "__root__"

    def build(self, *, force: bool = False) -> CodeGraphReport:
        files = self._files()
        fingerprint = self._current_fingerprint(files)
        if not force and fingerprint == self._fingerprint:
            return self.report()
        self.symbols = {}
        self.edges = []
        self.parse_errors = []
        calls: list[tuple[str, str, str, int]] = []
        imports: list[tuple[str, str, int]] = []
        for path in files:
            relative = path.relative_to(self.project_root)
            data = path.read_bytes()
            content_hash = hashlib.sha256(data).hexdigest()
            module = self._module(relative)
            try:
                tree = ast.parse(data.decode("utf-8"), filename=str(relative))
            except (SyntaxError, UnicodeDecodeError) as exc:
                self.parse_errors.append({"path": relative.as_posix(), "error": str(exc)})
                continue
            visitor = _PythonVisitor(module, relative.as_posix(), content_hash)
            visitor.visit(tree)
            self.symbols.update((symbol.id, symbol) for symbol in visitor.symbols)
            calls.extend((source, module, target, line) for source, target, line in visitor.calls)
            imports.extend((module, target, line) for target, line in visitor.imports)

        by_name: dict[str, list[str]] = {}
        by_module_name: dict[tuple[str, str], list[str]] = {}
        for symbol in self.symbols.values():
            by_name.setdefault(symbol.name, []).append(symbol.id)
            by_module_name.setdefault((symbol.module, symbol.name), []).append(symbol.id)
        for source, module, called, line in calls:
            short = called.rsplit(".", 1)[-1]
            local = by_module_name.get((module, short), [])
            candidates = local or by_name.get(short, [])
            if len(candidates) == 1:
                target = candidates[0]
                resolved = True
            else:
                target = f"external:{called}"
                resolved = False
            self.edges.append(CodeEdge(source, target, "calls", resolved, line))
        modules = {symbol.module for symbol in self.symbols.values()}
        for module, imported, line in imports:
            normalized = imported.lstrip(".")
            target = next(
                (candidate for candidate in modules if candidate == normalized or candidate.startswith(normalized + ".")),
                f"external-module:{imported}",
            )
            self.edges.append(CodeEdge(f"module:{module}", f"module:{target}", "imports", target in modules, line))
        self.file_count = len(files)
        self._fingerprint = fingerprint
        return self.report()

    def report(self) -> CodeGraphReport:
        return CodeGraphReport(
            language="python",
            files=self.file_count,
            symbols=len(self.symbols),
            edges=len(self.edges),
            unresolved_calls=sum(edge.kind == "calls" and not edge.resolved for edge in self.edges),
            parse_errors=tuple(self.parse_errors),
        )

    def search(self, query: str, *, limit: int = 20) -> list[CodeSymbol]:
        self.build()
        needle = query.casefold()
        values = [
            symbol for symbol in self.symbols.values()
            if needle in symbol.name.casefold()
            or needle in symbol.qualified_name.casefold()
            or needle in symbol.path.casefold()
        ]
        values.sort(key=lambda item: (item.name.casefold() != needle, item.qualified_name))
        return values[:limit]

    def resolve(self, symbol: str) -> CodeSymbol:
        self.build()
        if symbol in self.symbols:
            return self.symbols[symbol]
        matches = [
            value for value in self.symbols.values()
            if value.qualified_name == symbol or value.name == symbol
        ]
        if len(matches) != 1:
            raise KeyError(f"symbol must resolve uniquely: {symbol}")
        return matches[0]

    def context(self, symbol: str) -> dict[str, Any]:
        value = self.resolve(symbol)
        path = (self.project_root / value.path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("indexed path escapes project root")
        lines = path.read_text(encoding="utf-8").splitlines()
        return {
            "symbol": asdict(value),
            "content": "\n".join(lines[value.line - 1:value.end_line]),
            "outgoing": [asdict(edge) for edge in self.edges if edge.source == value.id],
            "incoming": [asdict(edge) for edge in self.edges if edge.target == value.id],
        }

    def impact(self, symbol: str, *, depth: int = 3) -> dict[str, Any]:
        if depth < 0:
            raise ValueError("depth cannot be negative")
        root = self.resolve(symbol)
        reverse: dict[str, set[str]] = {}
        for edge in self.edges:
            if edge.kind == "calls" and edge.resolved:
                reverse.setdefault(edge.target, set()).add(edge.source)
        levels: list[list[CodeSymbol]] = []
        seen = {root.id}
        frontier = {root.id}
        for _ in range(depth):
            next_ids = {source for target in frontier for source in reverse.get(target, ())} - seen
            if not next_ids:
                break
            levels.append([self.symbols[item] for item in sorted(next_ids)])
            seen.update(next_ids)
            frontier = next_ids
        return {
            "root": asdict(root),
            "reverse_callers_by_depth": [
                [asdict(symbol_value) for symbol_value in level] for level in levels
            ],
            "affected_symbol_ids": sorted(seen - {root.id}),
        }
