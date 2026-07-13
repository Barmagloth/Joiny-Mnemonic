from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


CONFIG_VERSION = 2
AGENTS = frozenset({"claude-code", "codex", "opencode", "openhands"})
PLUGINS = frozenset({"semantic-local", "knowledge-graph", "nuextract-local"})


def global_config_path(
    *, environ: Mapping[str, str] | None = None, home: str | Path | None = None
) -> Path:
    env = os.environ if environ is None else environ
    if env.get("JOINY_MNEMONIC_HOME"):
        root = Path(os.path.expandvars(os.path.expanduser(env["JOINY_MNEMONIC_HOME"])))
    else:
        root = Path(home).expanduser() if home is not None else Path.home()
        root = root / ".joiny-mnemonic"
    return root.resolve() / "config.json"


def project_config_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / ".joiny-mnemonic" / "config.json"


def validate_configuration(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    version = int(result.get("version", 0))
    if version not in {1, CONFIG_VERSION}:
        raise ValueError(f"unsupported installer configuration version: {version}")
    result["version"] = CONFIG_VERSION
    scope = result.get("scope", "default")
    if scope not in {"project", "global", "default"}:
        raise ValueError("configuration scope must be project or global")
    agents = result.get("agents", [])
    plugins = result.get("plugins", [])
    if not isinstance(agents, list) or not set(agents) <= AGENTS:
        raise ValueError("configuration contains unsupported agents")
    if not isinstance(plugins, list) or not set(plugins) <= PLUGINS:
        raise ValueError("configuration contains unsupported plugins")
    extractor = result.get("extractor", {})
    if not isinstance(extractor, dict):
        raise ValueError("extractor configuration must be an object")
    requested_enabled = bool(
        extractor.get("requested_enabled", extractor.get("enabled", False))
    )
    name = extractor.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError("extractor name must be a non-empty string")
    if requested_enabled and name is None:
        raise ValueError("requested extractor activation requires a plugin name")
    result["agents"] = sorted(set(str(item) for item in agents))
    result["plugins"] = sorted(set(str(item) for item in plugins))
    result["extractor"] = {
        "requested_enabled": requested_enabled,
        "name": name.strip() if isinstance(name, str) else None,
    }
    return result


def read_configuration(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Joiny-Mnemonic configuration: {target}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Joiny-Mnemonic configuration must be an object: {target}")
    return validate_configuration(value)


def write_configuration(path: str | Path, value: Mapping[str, Any]) -> Path:
    target = Path(path)
    validated = validate_configuration(value)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    try:
        temporary.replace(target)
    except PermissionError:
        target.write_bytes(data)
        temporary.unlink(missing_ok=True)
    return target


def effective_configuration(
    project_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> dict[str, Any]:
    global_value = read_configuration(global_config_path(environ=environ, home=home))
    project_value = read_configuration(project_config_path(project_root))
    if project_value is not None:
        return project_value
    if global_value is not None:
        return global_value
    return {
        "version": CONFIG_VERSION,
        "scope": "default",
        "agents": [],
        "plugins": [],
        "extractor": {"requested_enabled": False, "name": None},
    }
