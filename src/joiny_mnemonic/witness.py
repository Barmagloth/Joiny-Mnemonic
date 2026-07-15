from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class WitnessRegistry:
    """Best-effort independent local checkpoint; deliberately not a trust anchor.

    Storage layout (task6 packet-assembly fix): one shard file per project
    under ``witnesses.d/`` next to the legacy monolithic ``witnesses.json``.
    The hot path (every hook delivery checkpoints the chain head) reads and
    rewrites only the current project's few-hundred-byte shard — O(1) in the
    number of projects the machine has ever seen. The legacy monolith is
    consulted read-only as a migration fallback the first time a project
    without a shard is checked, so witnessed heads (rollback/divergence
    detection) survive the layout change; it is never rewritten.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = os.environ.get("JOINY_MNEMONIC_WITNESS_REGISTRY") or (
                Path.home() / ".joiny-mnemonic" / "witnesses.json"
            )
        self.path = Path(path).expanduser().resolve()
        self.shard_dir = self.path.with_suffix(".d")

    # --- legacy monolith (read-only fallback + registry-wide scans) ---------

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

    # --- per-project shards (hot path) --------------------------------------

    def _shard_path(self, project_id: str) -> Path:
        name = (
            project_id
            if _SAFE_ID.match(project_id)
            else hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        )
        return self.shard_dir / f"{name}.json"

    def _read_project(self, project_id: str) -> dict[str, Any] | None:
        """The project's witness entry: shard first, legacy monolith as a
        read-only migration fallback. None means never witnessed."""
        shard = self._shard_path(project_id)
        if shard.exists():
            value = json.loads(shard.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(
                value.get("chains", {}), dict
            ):
                raise ValueError("invalid witness shard")
            return value
        legacy = self._read()
        if legacy is None:
            return None
        project = legacy.get("projects", {}).get(project_id)
        if project is not None and not isinstance(project, dict):
            raise ValueError("invalid witness registry")
        return project

    def _write_project(self, project_id: str, project: dict[str, Any]) -> None:
        shard = self._shard_path(project_id)
        shard.parent.mkdir(parents=True, exist_ok=True)
        temporary = shard.with_suffix(shard.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                project,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(shard)

    def _all_projects(self) -> dict[str, dict[str, Any]]:
        """Registry-wide view (init-time scans only): legacy monolith
        entries, shadowed by shards where both exist."""
        projects: dict[str, dict[str, Any]] = {}
        try:
            legacy = self._read()
        except (OSError, ValueError, json.JSONDecodeError):
            legacy = None
        if legacy is not None:
            projects.update(
                {
                    key: value
                    for key, value in legacy.get("projects", {}).items()
                    if isinstance(value, dict)
                }
            )
        if self.shard_dir.is_dir():
            for shard in self.shard_dir.glob("*.json"):
                try:
                    value = json.loads(shard.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and value.get("project_instance_id"):
                    projects[str(value["project_instance_id"])] = value
        return projects

    def known_project_database_missing(
        self, canonical_path: str | Path
    ) -> tuple[dict[str, Any], ...]:
        target = str(Path(canonical_path).resolve())
        findings = []
        for project_id, project in self._all_projects().items():
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
            project = self._read_project(project_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "status": "external_witness_unreadable",
                "finding": "external_witness_missing",
                "details": {"error": type(exc).__name__},
            }
        if project is None:
            if not allow_first:
                return {
                    "status": "external_witness_missing",
                    "finding": "external_witness_missing",
                    "details": {"project_instance_id": project_id},
                }
            project = {
                "project_instance_id": project_id,
                "repository_identity": identity.get("repository_identity", ""),
                "canonical_path": identity.get("canonical_path", ""),
                "bootstrap_hash": identity["bootstrap_hash"],
                "first_seen_at": _now(),
                "chains": {},
            }

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
        # Migration completeness: a shard written from a legacy monolith
        # entry must carry the project id so registry-wide scans see it.
        project.setdefault("project_instance_id", project_id)
        try:
            self._write_project(project_id, project)
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
