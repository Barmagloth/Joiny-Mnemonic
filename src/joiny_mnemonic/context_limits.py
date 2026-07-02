from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from .models import BudgetPolicy


POLICY_FIELDS = (
    "context_window_tokens",
    "snapshot_ratio",
    "compact_ratio",
    "handoff_ratio",
    "hard_limit_ratio",
    "recommended_handoff_tokens",
    "reserve_tokens",
    "min_action_interval_events",
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(data, encoding="utf-8")
    try:
        temporary.replace(path)
    except PermissionError:
        path.write_text(data, encoding="utf-8")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_builtin_profiles() -> dict[str, Any]:
    resource = files("joiny_mnemonic").joinpath("context_limit_presets.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("profiles"), dict):
        raise RuntimeError("invalid bundled context limit profiles")
    return value


def global_limits_path(
    *, environ: Mapping[str, str] | None = None, home: str | Path | None = None
) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("JOINY_MNEMONIC_LIMITS_FILE")
    if explicit:
        return Path(os.path.expandvars(os.path.expanduser(explicit))).resolve()
    root = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    return root / ".joiny-mnemonic" / "context-limits.json"


def _as_number(value: Any, *, integer: bool, name: str) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    return result


def validate_limits(values: Mapping[str, Any]) -> dict[str, int | float]:
    missing = [name for name in POLICY_FIELDS if name not in values]
    if missing:
        raise ValueError("missing context limit fields: " + ", ".join(missing))
    normalized: dict[str, int | float] = {
        "context_window_tokens": _as_number(
            values["context_window_tokens"], integer=True, name="context_window_tokens"
        ),
        "snapshot_ratio": _as_number(
            values["snapshot_ratio"], integer=False, name="snapshot_ratio"
        ),
        "compact_ratio": _as_number(
            values["compact_ratio"], integer=False, name="compact_ratio"
        ),
        "handoff_ratio": _as_number(
            values["handoff_ratio"], integer=False, name="handoff_ratio"
        ),
        "hard_limit_ratio": _as_number(
            values["hard_limit_ratio"], integer=False, name="hard_limit_ratio"
        ),
        "recommended_handoff_tokens": _as_number(
            values["recommended_handoff_tokens"],
            integer=True,
            name="recommended_handoff_tokens",
        ),
        "reserve_tokens": _as_number(
            values["reserve_tokens"], integer=True, name="reserve_tokens"
        ),
        "min_action_interval_events": _as_number(
            values["min_action_interval_events"],
            integer=True,
            name="min_action_interval_events",
        ),
    }
    window = int(normalized["context_window_tokens"])
    reserve = int(normalized["reserve_tokens"])
    handoff_cap = int(normalized["recommended_handoff_tokens"])
    snapshot = float(normalized["snapshot_ratio"])
    compact = float(normalized["compact_ratio"])
    handoff = float(normalized["handoff_ratio"])
    hard = float(normalized["hard_limit_ratio"])
    interval = int(normalized["min_action_interval_events"])
    if window <= 0 or reserve < 0 or reserve >= window or handoff_cap <= 0 or interval < 0:
        raise ValueError(
            "token limits must be positive and reserve must be smaller than the context window"
        )
    if not (0 < snapshot < compact < handoff < hard <= 1):
        raise ValueError("budget ratios must be strictly increasing and at most 1")
    physical_handoff = min(math.ceil(window * handoff), window - reserve)
    hard_threshold = min(math.ceil(window * hard), window - reserve)
    if min(handoff_cap, physical_handoff) >= hard_threshold:
        raise ValueError("recommended handoff must leave room before the hard limit")
    return normalized


class ContextLimitConfig:
    """Resolve immutable built-in presets into editable per-agent JSON configuration."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        environ: Mapping[str, str] | None = None,
        home: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.environ = os.environ if environ is None else environ
        self.home = home
        self.builtins = load_builtin_profiles()
        self._cache: dict[Path, tuple[int, int, dict[str, Any]]] = {}

    @property
    def project_path(self) -> Path:
        return self.project_root / ".joiny-mnemonic" / "context-limits.json"

    @property
    def global_path(self) -> Path:
        return global_limits_path(environ=self.environ, home=self.home)

    def _document(self, path: Path) -> dict[str, Any]:
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._cache.pop(path, None)
            return {}
        cached = self._cache.get(path)
        key = (stat.st_mtime_ns, stat.st_size)
        if cached is not None and cached[:2] == key:
            return cached[2]
        document = _read_json(path)
        self._cache[path] = (key[0], key[1], document)
        return document

    def profiles(self) -> dict[str, Any]:
        return dict(self.builtins["profiles"])

    def default_profile(self, agent: str) -> str:
        try:
            return str(self.builtins["agent_defaults"][agent])
        except KeyError as exc:
            raise ValueError(f"no default context profile for agent: {agent}") from exc

    def configure_agent(
        self,
        agent: str,
        *,
        profile: str | None = None,
        global_scope: bool = False,
        overrides: Mapping[str, Any] | None = None,
    ) -> tuple[Path, BudgetPolicy]:
        path = self.global_path if global_scope else self.project_path
        document = _read_json(path)
        if document and document.get("schema_version") != 1:
            raise ValueError(f"unsupported context limits schema in {path}")
        agents = document.setdefault("agents", {})
        if not isinstance(agents, dict):
            raise ValueError(f"agents in {path} must be an object")
        supplied_overrides = {
            key: value for key, value in (overrides or {}).items() if value is not None
        }
        existing = agents.get(agent)
        if profile is None and not supplied_overrides and isinstance(existing, dict):
            existing_limits = existing.get("limits")
            if isinstance(existing_limits, dict):
                normalized = validate_limits(existing_limits)
                selected = str(existing.get("profile", "custom"))
                return path, self._policy(
                    agent, selected, normalized, branch_id="main", source=str(path)
                )

        selected = profile or self.default_profile(agent)
        if selected == "custom":
            base: dict[str, Any] = {
                "context_window_tokens": 128000,
                "snapshot_ratio": 0.30,
                "compact_ratio": 0.50,
                "handoff_ratio": 0.70,
                "hard_limit_ratio": 0.90,
                "recommended_handoff_tokens": 64000,
                "reserve_tokens": 16000,
                "min_action_interval_events": 20,
            }
        else:
            try:
                base = dict(self.builtins["profiles"][selected])
            except KeyError as exc:
                available = ", ".join(sorted(self.builtins["profiles"]))
                raise ValueError(
                    f"unknown context profile {selected!r}; available: {available}, custom"
                ) from exc
        for key, value in supplied_overrides.items():
            if key not in POLICY_FIELDS:
                raise ValueError(f"unknown context limit override: {key}")
            base[key] = value
        normalized = validate_limits(base)
        agents[agent] = {
            "profile": selected,
            "limits": normalized,
        }
        document["schema_version"] = 1
        _write_json(path, document)
        self._cache.pop(path, None)
        return path, self._policy(
            agent,
            selected,
            normalized,
            branch_id="main",
            source=str(path),
        )
    def resolve(
        self,
        agent: str,
        *,
        branch_id: str = "main",
    ) -> BudgetPolicy | None:
        for path in (self.project_path, self.global_path):
            document = self._document(path)
            if not document:
                continue
            if document.get("schema_version") != 1:
                raise ValueError(f"unsupported context limits schema in {path}")
            agents = document.get("agents", {})
            if not isinstance(agents, dict) or agent not in agents:
                continue
            entry = agents[agent]
            if not isinstance(entry, dict) or not isinstance(entry.get("limits"), dict):
                raise ValueError(f"invalid context limits for {agent} in {path}")
            profile = str(entry.get("profile", "custom"))
            normalized = validate_limits(entry["limits"])
            return self._policy(agent, profile, normalized, branch_id=branch_id, source=str(path))
        return None

    @staticmethod
    def _policy(
        agent: str,
        profile: str,
        values: Mapping[str, int | float],
        *,
        branch_id: str,
        source: str,
    ) -> BudgetPolicy:
        fingerprint = json.dumps(dict(values), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{profile}:{fingerprint}".encode()).hexdigest()[:20]
        return BudgetPolicy(
            id=f"config:{agent}:{digest}",
            branch_id=branch_id,
            version=1,
            context_window_tokens=int(values["context_window_tokens"]),
            snapshot_ratio=float(values["snapshot_ratio"]),
            compact_ratio=float(values["compact_ratio"]),
            handoff_ratio=float(values["handoff_ratio"]),
            hard_limit_ratio=float(values["hard_limit_ratio"]),
            min_action_interval_events=int(values["min_action_interval_events"]),
            created_at="config",
            agent=agent,
            profile=profile,
            recommended_handoff_tokens=int(values["recommended_handoff_tokens"]),
            reserve_tokens=int(values["reserve_tokens"]),
            source=source,
        )
