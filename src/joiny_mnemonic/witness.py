from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class WitnessRegistry:
    """Best-effort independent local checkpoint; deliberately not a trust anchor."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path).expanduser().resolve()
            if path is not None
            else (Path.home() / ".joiny-mnemonic" / "witnesses.json").resolve()
        )

    def _read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("projects", {}), dict):
            raise ValueError("invalid witness registry")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(
            self.path.suffix + f".{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def known_project_database_missing(
        self, canonical_path: str | Path
    ) -> tuple[dict[str, Any], ...]:
        try:
            registry = self._read()
        except (OSError, ValueError, json.JSONDecodeError):
            return ()
        if registry is None:
            return ()
        target = str(Path(canonical_path).resolve())
        findings = []
        for project_id, project in registry.get("projects", {}).items():
            if project.get("canonical_path") != target:
                continue
            databases = (
                Path(target) / ".joiny-mnemonic" / "memory.db",
                Path(target) / ".llm-memory" / "memory.db",
            )
            if not any(database.exists() for database in databases):
                findings.append({
                    "finding": "known_project_database_missing",
                    "project_instance_id": project_id,
                    "canonical_path": target,
                })
        return tuple(findings)
    def check_and_update(
        self, store: Any, *, allow_first: bool = False
    ) -> dict[str, Any]:
        identity = store.project_identity()
        if identity is None:
            return {"status": "uninitialized", "finding": None}
        checkpoint = store.chain_checkpoint()
        project_id = identity["project_instance_id"]
        chain_id = identity["chain_id"]
        try:
            registry = self._read()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "status": "external_witness_unreadable",
                "finding": "external_witness_missing",
                "details": {"error": type(exc).__name__},
            }
        if registry is None:
            if not allow_first:
                return {
                    "status": "external_witness_missing",
                    "finding": "external_witness_missing",
                    "details": {"project_instance_id": project_id},
                }
            registry = {"version": 1, "projects": {}}

        projects = registry.setdefault("projects", {})
        project = projects.get(project_id)
        if project is None:
            if not allow_first:
                return {
                    "status": "external_witness_missing",
                    "finding": "external_witness_missing",
                    "details": {"project_instance_id": project_id},
                }
            project = {
                "repository_identity": identity.get("repository_identity", ""),
                "canonical_path": identity.get("canonical_path", ""),
                "bootstrap_hash": identity["bootstrap_hash"],
                "first_seen_at": _now(),
                "chains": {},
            }
            projects[project_id] = project

        if project.get("bootstrap_hash") != identity["bootstrap_hash"]:
            return {
                "status": "policy_rebootstrapped",
                "finding": "policy_rebootstrapped",
                "details": {
                    "witnessed_bootstrap_hash": project.get("bootstrap_hash"),
                    "current_bootstrap_hash": identity["bootstrap_hash"],
                },
            }
        chains = project.setdefault("chains", {})
        witnessed = chains.get(chain_id)
        if witnessed is None and chains:
            return {
                "status": "undeclared_chain_replacement",
                "finding": "undeclared_chain_replacement",
                "details": {
                    "project_instance_id": project_id,
                    "chain_id": chain_id,
                    "known_chain_ids": sorted(chains),
                },
            }

        status = "first_checkpoint"
        if witnessed is not None:
            witnessed_seq = int(witnessed["head_seq"])
            current_seq = int(checkpoint["head_seq"])
            if current_seq < witnessed_seq:
                return {
                    "status": "history_rollback",
                    "finding": "history_rollback",
                    "details": {
                        "witnessed_seq": witnessed_seq,
                        "current_seq": current_seq,
                        "witnessed_hash": witnessed["head_hash"],
                    },
                }
            actual = store.chain_hash_at(witnessed_seq)
            if actual != witnessed["head_hash"]:
                return {
                    "status": "history_divergence",
                    "finding": "history_divergence",
                    "details": {
                        "witnessed_seq": witnessed_seq,
                        "witnessed_hash": witnessed["head_hash"],
                        "current_hash_at_witnessed_seq": actual,
                    },
                }
            status = "valid_extension"

        now = _now()
        chains[chain_id] = {
            "project_instance_id": project_id,
            "chain_id": chain_id,
            "head_seq": checkpoint["head_seq"],
            "head_hash": checkpoint["head_hash"],
            "bootstrap_hash": identity["bootstrap_hash"],
            "first_seen_at": (
                witnessed.get("first_seen_at", now) if witnessed else now
            ),
            "last_seen_at": now,
        }
        try:
            self._write(registry)
        except OSError as exc:
            return {
                "status": "registry_update_failed",
                "finding": None,
                "details": {
                    "valid_extension": status in {"valid_extension", "first_checkpoint"},
                    "error": type(exc).__name__,
                },
            }
        return {"status": status, "finding": None, "details": chains[chain_id]}