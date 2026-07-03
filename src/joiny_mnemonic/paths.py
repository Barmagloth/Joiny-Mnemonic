from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

STATE_DIRECTORY = ".joiny-mnemonic"
LEGACY_STATE_DIRECTORY = ".llm-memory"
DATABASE_FILENAME = "memory.db"

PROJECT_ROOT_ENVIRONMENT = (
    "JOINY_MNEMONIC_PROJECT_ROOT",
    "CLAUDE_PROJECT_DIR",
    "CODEX_PROJECT_DIR",
    "OPENHANDS_PROJECT_DIR",
)


def resolve_runtime_project(
    project_root: str | Path | None = ".",
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a relative CLI root using the project identity supplied by the host."""
    env = os.environ if environ is None else environ
    raw = "." if project_root is None else str(project_root).strip()
    if raw in {"", "."}:
        raw = next(
            (env[name] for name in PROJECT_ROOT_ENVIRONMENT if env.get(name)),
            raw or ".",
        )
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()


def resolve_runtime_database(
    database: str | Path | None,
    project_root: str | Path,
) -> str | Path:
    """Resolve a relative database path against the resolved project, not process cwd."""
    if database is None:
        return resolve_project_database(project_root)
    if str(database) == ":memory:":
        return ":memory:"
    path = Path(os.path.expandvars(os.path.expanduser(str(database))))
    if not path.is_absolute():
        path = Path(project_root) / path
    return path.resolve()


def resolve_project_database(project_root: str | Path) -> Path:
    """Prefer the renamed state path while preserving existing legacy databases."""
    root = Path(project_root).resolve()
    current = root / STATE_DIRECTORY / DATABASE_FILENAME
    legacy = root / LEGACY_STATE_DIRECTORY / DATABASE_FILENAME
    if current.exists() or not legacy.exists():
        return current
    return legacy