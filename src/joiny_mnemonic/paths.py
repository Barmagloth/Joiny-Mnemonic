from __future__ import annotations

from pathlib import Path

STATE_DIRECTORY = ".joiny-mnemonic"
LEGACY_STATE_DIRECTORY = ".llm-memory"
DATABASE_FILENAME = "memory.db"


def resolve_project_database(project_root: str | Path) -> Path:
    """Prefer the renamed state path while preserving existing legacy databases."""
    root = Path(project_root).resolve()
    current = root / STATE_DIRECTORY / DATABASE_FILENAME
    legacy = root / LEGACY_STATE_DIRECTORY / DATABASE_FILENAME
    if current.exists() or not legacy.exists():
        return current
    return legacy