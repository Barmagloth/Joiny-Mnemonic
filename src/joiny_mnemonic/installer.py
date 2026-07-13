from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .configuration import (
    AGENTS,
    CONFIG_VERSION,
    PLUGINS,
    global_config_path,
    project_config_path,
    read_configuration,
    write_configuration,
)
from .hooks import install_hooks, uninstall_hooks
from .paths import resolve_project_database


REPOSITORY_URL = "https://github.com/Barmagloth/Joiny-Mnemonic.git"


@dataclass(frozen=True, slots=True)
class AgentDetection:
    id: str
    label: str
    command: str
    executable: str | None
    config_detected: bool

    @property
    def detected(self) -> bool:
        return self.executable is not None or self.config_detected


@dataclass(frozen=True, slots=True)
class SetupResult:
    scope: str
    project_root: str
    agents: tuple[str, ...]
    plugins: tuple[str, ...]
    hooks: tuple[dict[str, Any], ...]
    mcp: tuple[dict[str, Any], ...]
    plugin_installs: tuple[dict[str, Any], ...]
    configuration_file: str
    database: str | None
    dry_run: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UninstallSummary:
    scope: str
    project_root: str
    agents: tuple[str, ...]
    hooks: tuple[dict[str, Any], ...]
    mcp: tuple[dict[str, Any], ...]
    configuration_file: str
    configuration_removed: bool
    data_preserved: tuple[str, ...]
    data_deleted: tuple[str, ...]
    dry_run: bool
    notes: tuple[str, ...] = ()


AGENT_METADATA = {
    "claude-code": ("Claude Code", "claude", (".claude",)),
    "codex": ("Codex", "codex", (".codex",)),
    "opencode": ("OpenCode", "opencode", (".opencode", "opencode.json")),
    "openhands": ("OpenHands", "openhands", (".openhands",)),
}

PLUGIN_METADATA = {
    "semantic-local": ("Semantic search", "plugins/semantic-local"),
    "knowledge-graph": ("Knowledge graph", "plugins/knowledge-graph"),
    "nuextract-local": ("NuExtract local extractor", "plugins/nuextract-local"),
}

GLOBAL_AGENT_MARKERS = {
    "claude-code": (".claude",),
    "codex": (".codex",),
    "opencode": (".config/opencode",),
    "openhands": (".openhands",),
}


def detect_agents(
    project_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> tuple[AgentDetection, ...]:
    env = os.environ if environ is None else environ
    root = Path(project_root).expanduser().resolve()
    user_home = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    path = env.get("PATH")
    result = []
    for identifier, (label, command, markers) in AGENT_METADATA.items():
        executable = shutil.which(command, path=path)
        config_detected = any((root / marker).exists() for marker in markers)
        if not config_detected:
            config_detected = any(
                (user_home / marker).exists()
                for marker in GLOBAL_AGENT_MARKERS[identifier]
            )
        result.append(
            AgentDetection(identifier, label, command, executable, config_detected)
        )
    return tuple(result)


def plugin_install_spec(plugin: str, source_root: str | Path | None) -> str:
    if plugin not in PLUGIN_METADATA:
        raise ValueError(f"unsupported optional component: {plugin}")
    relative = PLUGIN_METADATA[plugin][1]
    candidate_root = (
        Path(source_root).expanduser().resolve()
        if source_root is not None
        else Path(__file__).resolve().parents[2]
    )
    local = candidate_root / relative
    if (local / "pyproject.toml").is_file():
        return str(local)
    return f"git+{REPOSITORY_URL}@main#subdirectory={relative}"


def mcp_command(
    agent: str,
    project_root: str | Path,
    *,
    scope: str,
    python_executable: str | None = None,
) -> list[str] | None:
    python = python_executable or sys.executable
    root = Path(project_root).expanduser().resolve()
    server = [python, "-m", "joiny_mnemonic"]
    if scope == "project":
        server += [
            "--db", str(resolve_project_database(root)),
            "--project-root", str(root),
        ]
    else:
        server += ["--project-root", "."]
    server.append("mcp")
    if agent == "claude-code":
        claude_scope = "local" if scope == "project" else "user"
        return [
            "claude", "mcp", "add", "--transport", "stdio", "--scope",
            claude_scope, "joiny-mnemonic", "--", *server,
        ]
    if agent == "codex":
        return ["codex", "mcp", "add", "joiny-mnemonic", "--", *server]
    if agent == "openhands":
        return [
            "openhands", "mcp", "add", "joiny-mnemonic", "--transport",
            "stdio", *server,
        ]
    return None


def mcp_remove_command(agent: str, *, scope: str) -> list[str] | None:
    if scope not in {"project", "global"}:
        raise ValueError("scope must be project or global")
    if agent == "claude-code":
        claude_scope = "local" if scope == "project" else "user"
        return [
            "claude", "mcp", "remove", "--scope", claude_scope,
            "joiny-mnemonic",
        ]
    if agent == "codex":
        return ["codex", "mcp", "remove", "joiny-mnemonic"]
    if agent == "openhands":
        return ["openhands", "mcp", "remove", "joiny-mnemonic"]
    return None


def _nested_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return tuple(
            item
            for child in value.values()
            for item in _nested_strings(child)
        )
    if isinstance(value, (list, tuple)):
        return tuple(item for child in value for item in _nested_strings(child))
    return ()


def _codex_registration_matches_project(value: Any, project_root: Path) -> bool:
    strings = _nested_strings(value)
    expected = (
        "joiny_mnemonic",
        str(project_root),
        str(resolve_project_database(project_root)),
    )
    return all(any(item in candidate for candidate in strings) for item in expected)


def _write_host_json(path: Path, value: dict[str, Any]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    original = path.read_bytes() if path.exists() else None
    backup = path.with_suffix(path.suffix + ".joiny-mnemonic.bak") if original else None
    if backup is not None:
        backup.write_bytes(original)
        if backup.read_bytes() != original:
            raise OSError(f"failed to verify backup for {path}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    try:
        temporary.replace(path)
    except PermissionError:
        path.write_bytes(data)
        temporary.unlink(missing_ok=True)
    try:
        rendered = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rendered, dict):
            raise ValueError("written host configuration is not an object")
    except (OSError, ValueError, json.JSONDecodeError):
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
        raise
    return backup


def _remove_opencode_mcp(
    project_root: Path,
    *,
    scope: str,
    home: Path,
    dry_run: bool,
) -> dict[str, Any]:
    path = (
        project_root / "opencode.json"
        if scope == "project"
        else home / ".config" / "opencode" / "opencode.json"
    )
    if not path.exists():
        return {"agent": "opencode", "status": "not-configured", "path": str(path)}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"OpenCode configuration must be an object: {path}")
    mcp = value.get("mcp")
    if not isinstance(mcp, dict) or "joiny-mnemonic" not in mcp:
        return {"agent": "opencode", "status": "not-configured", "path": str(path)}
    if dry_run:
        return {"agent": "opencode", "status": "planned", "path": str(path)}
    mcp.pop("joiny-mnemonic")
    if not mcp:
        value.pop("mcp", None)
    _write_host_json(path, value)
    return {"agent": "opencode", "status": "removed", "path": str(path)}


def _merge_opencode_mcp(
    project_root: Path,
    server_command: Sequence[str],
    *,
    scope: str,
    home: Path,
    dry_run: bool,
) -> Path:
    path = (
        project_root / "opencode.json"
        if scope == "project"
        else home / ".config" / "opencode" / "opencode.json"
    )
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"OpenCode configuration must be an object: {path}")
    else:
        value = {}
    mcp = value.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError(f"OpenCode mcp configuration must be an object: {path}")
    mcp["joiny-mnemonic"] = {
        "type": "local",
        "command": list(server_command),
        "enabled": True,
    }
    if not dry_run:
        _write_host_json(path, value)
    return path


def run_setup(
    project_root: str | Path,
    *,
    agents: Iterable[str],
    plugins: Iterable[str] = (),
    scope: str = "project",
    install_hook_adapters: bool = True,
    install_mcp: bool = False,
    install_plugins: bool = True,
    enable_extraction: bool = False,
    source_root: str | Path | None = None,
    dry_run: bool = False,
    python_executable: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> SetupResult:
    if scope not in {"project", "global"}:
        raise ValueError("scope must be project or global")
    selected_agents = tuple(dict.fromkeys(str(item) for item in agents))
    selected_plugins = tuple(dict.fromkeys(str(item) for item in plugins))
    if not set(selected_agents) <= AGENTS:
        raise ValueError("unsupported agent selection")
    if not set(selected_plugins) <= PLUGINS:
        raise ValueError("unsupported plugin selection")
    if enable_extraction and "nuextract-local" not in selected_plugins:
        raise ValueError("automatic extraction requires the nuextract-local component")
    if enable_extraction and scope != "project":
        raise ValueError("automatic extraction activation requires project scope")
    root = Path(project_root).expanduser().resolve()
    user_home = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    python = python_executable or sys.executable
    env = os.environ if environ is None else environ
    plugin_results: list[dict[str, Any]] = []
    hook_results: list[dict[str, Any]] = []
    mcp_results: list[dict[str, Any]] = []
    notes: list[str] = []

    for plugin in selected_plugins:
        spec = plugin_install_spec(plugin, source_root)
        command = [python, "-m", "pip", "install", spec]
        if dry_run or not install_plugins:
            status = "planned" if dry_run else "externally-managed"
            plugin_results.append({"plugin": plugin, "status": status, "command": command})
            continue
        completed = runner(command, check=False, capture_output=True, text=True)
        if completed.returncode:
            raise RuntimeError(
                f"failed to install {plugin}: {(completed.stderr or completed.stdout).strip()}"
            )
        plugin_results.append({"plugin": plugin, "status": "installed", "command": command})

    if install_hook_adapters:
        for agent in selected_agents:
            if scope == "global" and agent == "openhands":
                notes.append("OpenHands has no user-global hooks; its hook install was skipped.")
                continue
            if dry_run:
                hook_results.append({"agent": agent, "status": "planned", "scope": scope})
                continue
            hook_results.append(
                asdict(
                    install_hooks(
                        agent,
                        root,
                        global_scope=scope == "global",
                        environ=dict(environ) if environ is not None else None,
                        home=user_home,
                    )
                )
            )

    if install_mcp:
        for agent in selected_agents:
            command = mcp_command(
                agent, root, scope=scope, python_executable=python
            )
            if agent == "opencode":
                server = [python, "-m", "joiny_mnemonic"]
                if scope == "project":
                    server += [
                        "--db", str(resolve_project_database(root)),
                        "--project-root", str(root),
                    ]
                else:
                    server += ["--project-root", "."]
                server.append("mcp")
                path = _merge_opencode_mcp(
                    root, server, scope=scope, home=user_home, dry_run=dry_run
                )
                mcp_results.append(
                    {"agent": agent, "status": "planned" if dry_run else "configured", "path": str(path)}
                )
                continue
            if command is None:
                notes.append(f"{agent} MCP registration is not automated.")
                continue
            executable = shutil.which(command[0], path=env.get("PATH"))
            if executable is None:
                mcp_results.append({"agent": agent, "status": "not-installed", "command": command})
                continue
            if dry_run:
                mcp_results.append({"agent": agent, "status": "planned", "command": command})
                continue
            completed = runner(command, check=False, capture_output=True, text=True, cwd=root)
            if completed.returncode:
                raise RuntimeError(
                    f"failed to register MCP for {agent}: "
                    f"{(completed.stderr or completed.stdout).strip()}"
                )
            mcp_results.append({"agent": agent, "status": "configured", "command": command})
            if agent == "codex" and scope == "project":
                notes.append(
                    "Codex CLI stores MCP servers in user configuration; the server still targets this project."
                )

    config_path = (
        project_config_path(root)
        if scope == "project"
        else global_config_path(environ=environ, home=user_home)
    )
    config = {
        "version": CONFIG_VERSION,
        "scope": scope,
        "agents": list(selected_agents),
        "plugins": list(selected_plugins),
        "hooks_enabled": install_hook_adapters,
        "mcp_enabled": install_mcp,
        "extractor": {
            "requested_enabled": bool(enable_extraction),
            "name": "nuextract-local" if "nuextract-local" in selected_plugins else None,
        },
    }
    if not dry_run:
        write_configuration(config_path, config)

    database: str | None = None
    if scope == "project":
        database = str(resolve_project_database(root))
        if not dry_run:
            from .service import MemoryService

            with MemoryService(database, project_root=root) as service:
                if service.store.project_identity() is None:
                    service.initialize_project(
                        automatic_extraction_enabled=enable_extraction
                    )
                    if enable_extraction:
                        notes.append(
                            "Automatic extraction was enabled in the initial TOFU policy."
                        )
                elif enable_extraction:
                    active = service.store.active_policy()
                    assert active is not None
                    if active["policy"].get("automatic_extraction_enabled", False):
                        notes.append("Automatic extraction is already enabled by active policy.")
                    else:
                        requested_policy = dict(active["policy"])
                        requested_policy["automatic_extraction_enabled"] = True
                        pending = next(
                            (
                                event
                                for event in service.store.query_events(kinds=("state",))
                                if event.payload.get("operation")
                                == "policy_change_requested"
                                and event.payload.get("policy") == requested_policy
                                and event.payload.get("active_policy_id") == active["id"]
                            ),
                            None,
                        )
                        event = pending or service.request_policy_change(requested_policy)
                        notes.append(
                            "Automatic extraction remains disabled until trusted policy approval; "
                            f"request event: {event.id}."
                        )

    return SetupResult(
        scope=scope,
        project_root=str(root),
        agents=selected_agents,
        plugins=selected_plugins,
        hooks=tuple(hook_results),
        mcp=tuple(mcp_results),
        plugin_installs=tuple(plugin_results),
        configuration_file=str(config_path),
        database=database,
        dry_run=dry_run,
        notes=tuple(notes),
    )


def _project_data_paths(project_root: Path) -> tuple[Path, ...]:
    root = project_root.resolve()
    data_roots = (
        (root / ".joiny-mnemonic").resolve(),
        (root / ".llm-memory").resolve(),
    )
    candidates: list[Path] = []
    for data_root in data_roots:
        if not data_root.is_relative_to(root):
            raise ValueError(f"project data path escaped project root: {data_root}")
        database = data_root / "memory.db"
        candidates.extend(
            (
                database,
                Path(str(database) + "-wal"),
                Path(str(database) + "-shm"),
                data_root / "artifacts",
            )
        )
        candidates.extend(data_root.glob("memory.db.pre-migration-*.bak"))
    result: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if not any(
            resolved == allowed or resolved.is_relative_to(allowed)
            for allowed in data_roots
        ):
            raise ValueError(f"refusing to delete data outside project storage: {resolved}")
        if resolved.exists() and resolved not in result:
            result.append(resolved)
    return tuple(result)


def _delete_project_data(project_root: Path) -> tuple[str, ...]:
    targets = _project_data_paths(project_root)
    removed: list[str] = []
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        removed.append(str(target))
    return tuple(removed)


def run_uninstall(
    project_root: str | Path,
    *,
    agents: Iterable[str] = (),
    scope: str = "project",
    remove_hook_adapters: bool | None = None,
    remove_mcp: bool | None = None,
    delete_data: bool = False,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> UninstallSummary:
    if scope not in {"project", "global"}:
        raise ValueError("scope must be project or global")
    if delete_data and scope != "project":
        raise ValueError("durable data deletion is supported only for project scope")
    root = Path(project_root).expanduser().resolve()
    user_home = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    env = os.environ if environ is None else environ
    config_path = (
        project_config_path(root)
        if scope == "project"
        else global_config_path(environ=environ, home=user_home)
    )
    config = read_configuration(config_path)
    requested_agents = tuple(dict.fromkeys(str(item) for item in agents))
    selected_agents = requested_agents or tuple((config or {}).get("agents", ()))
    if not selected_agents:
        raise ValueError(
            "no configured products found; pass --agent to select products explicitly"
        )
    if not set(selected_agents) <= AGENTS:
        raise ValueError("unsupported agent selection")
    remove_hooks = (
        bool((config or {}).get("hooks_enabled", True))
        if remove_hook_adapters is None else remove_hook_adapters
    )
    remove_registered_mcp = (
        bool((config or {}).get("mcp_enabled", bool(requested_agents)))
        if remove_mcp is None else remove_mcp
    )

    hook_results: list[dict[str, Any]] = []
    mcp_results: list[dict[str, Any]] = []
    notes: list[str] = []
    incomplete = False
    if remove_hooks:
        for agent in selected_agents:
            if scope == "global" and agent == "openhands":
                notes.append("OpenHands has no user-global hooks.")
                continue
            hook_results.append(
                asdict(
                    uninstall_hooks(
                        agent,
                        root,
                        global_scope=scope == "global",
                        environ=dict(environ) if environ is not None else None,
                        home=user_home,
                        dry_run=dry_run,
                    )
                )
            )

    if remove_registered_mcp:
        for agent in selected_agents:
            if agent == "opencode":
                mcp_results.append(
                    _remove_opencode_mcp(
                        root, scope=scope, home=user_home, dry_run=dry_run
                    )
                )
                continue
            command = mcp_remove_command(agent, scope=scope)
            assert command is not None
            executable = shutil.which(command[0], path=env.get("PATH"))
            if executable is None:
                mcp_results.append(
                    {"agent": agent, "status": "not-installed", "command": command}
                )
                incomplete = True
                continue
            if dry_run:
                mcp_results.append(
                    {"agent": agent, "status": "planned", "command": command}
                )
                continue
            if agent == "codex" and scope == "project":
                inspection_command = [
                    "codex", "mcp", "get", "joiny-mnemonic", "--json"
                ]
                inspected = runner(
                    inspection_command,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=root,
                )
                if inspected.returncode:
                    detail = (inspected.stderr or inspected.stdout).strip()
                    if "not found" in detail.casefold() or "does not exist" in detail.casefold():
                        mcp_results.append(
                            {
                                "agent": agent,
                                "status": "not-configured",
                                "command": command,
                            }
                        )
                        continue
                    raise RuntimeError(f"failed to inspect MCP for {agent}: {detail}")
                try:
                    registration = json.loads(inspected.stdout)
                except json.JSONDecodeError:
                    mcp_results.append(
                        {
                            "agent": agent,
                            "status": "ownership-unverified",
                            "command": command,
                        }
                    )
                    incomplete = True
                    continue
                if not _codex_registration_matches_project(registration, root):
                    mcp_results.append(
                        {
                            "agent": agent,
                            "status": "ownership-mismatch",
                            "command": command,
                        }
                    )
                    incomplete = True
                    continue
            completed = runner(
                command, check=False, capture_output=True, text=True, cwd=root
            )
            if completed.returncode:
                detail = (completed.stderr or completed.stdout).strip()
                if "not found" in detail.casefold() or "does not exist" in detail.casefold():
                    mcp_results.append(
                        {"agent": agent, "status": "not-configured", "command": command}
                    )
                    continue
                raise RuntimeError(f"failed to remove MCP for {agent}: {detail}")
            mcp_results.append(
                {"agent": agent, "status": "removed", "command": command}
            )

    configuration_removed = False
    if config_path.exists() and not dry_run and not incomplete:
        config_path.unlink()
        configuration_removed = True
    elif config_path.exists() and incomplete:
        notes.append(
            "Installer configuration was retained because MCP cleanup could not be "
            "verified safely; inspect the reported status and rerun uninstall."
        )
    data_preserved: tuple[str, ...] = ()
    data_deleted: tuple[str, ...] = ()
    if scope == "project":
        existing_data = _project_data_paths(root)
        if delete_data and incomplete:
            data_preserved = tuple(str(path) for path in existing_data)
            notes.append(
                "Durable data was not deleted because integration cleanup is incomplete."
            )
        elif delete_data and dry_run:
            data_preserved = tuple(str(path) for path in existing_data)
            notes.append(
                "Dry run: durable database, sidecars, migration backups and artifacts "
                "would be deleted."
            )
        elif delete_data:
            data_deleted = _delete_project_data(root)
            notes.append("Durable project data was explicitly deleted.")
        else:
            data_preserved = tuple(str(path) for path in existing_data)
            notes.append("Durable memory data is preserved by default.")
    return UninstallSummary(
        scope=scope,
        project_root=str(root),
        agents=selected_agents,
        hooks=tuple(hook_results),
        mcp=tuple(mcp_results),
        configuration_file=str(config_path),
        configuration_removed=configuration_removed,
        data_preserved=data_preserved,
        data_deleted=data_deleted,
        dry_run=dry_run,
        notes=tuple(notes),
    )


def _select_indices(
    prompt: str,
    *,
    default: str,
    maximum: int,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> set[int]:
    while True:
        raw = input_fn(prompt).strip() or default
        if not raw:
            return set()
        try:
            values = {int(value.strip()) for value in raw.split(",") if value.strip()}
        except ValueError:
            output_fn("Invalid selection; enter comma-separated numbers.")
            continue
        if any(value < 1 or value > maximum for value in values):
            output_fn(f"Invalid selection; choose numbers from 1 to {maximum}.")
            continue
        return values


def _ask_yes_no(
    prompt: str,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    default: bool = False,
) -> bool:
    while True:
        value = input_fn(prompt).strip().casefold()
        if not value:
            return default
        if value in {"n", "no"}:
            return False
        if value in {"y", "yes"}:
            return True
        output_fn("Please answer y or n.")


def confirm_data_deletion(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> bool:
    output_fn(
        "Durable data includes memory.db, SQLite sidecars, migration backups and artifacts."
    )
    return _ask_yes_no(
        "Delete durable project data too? [y/N]: ",
        input_fn=input_fn,
        output_fn=output_fn,
    )


def select_interactively(
    detections: Sequence[AgentDetection],
    *,
    default_scope: str = "project",
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> tuple[tuple[str, ...], tuple[str, ...], bool, str, bool]:
    output_fn("Detected LLM products:")
    for index, item in enumerate(detections, 1):
        marker = "x" if item.detected else " "
        detail = item.executable or (
            "configuration found" if item.config_detected else "not detected"
        )
        output_fn(f"  {index}. [{marker}] {item.label} - {detail}")
    defaults = ",".join(
        str(index) for index, item in enumerate(detections, 1) if item.detected
    )
    indices = _select_indices(
        f"Products to configure [{defaults or 'none'}]: ",
        default=defaults,
        maximum=len(detections),
        input_fn=input_fn,
        output_fn=output_fn,
    )
    agents = tuple(
        item.id for index, item in enumerate(detections, 1) if index in indices
    )

    output_fn("Optional components (installation only; activation is separate):")
    plugin_ids = tuple(PLUGIN_METADATA)
    for index, identifier in enumerate(plugin_ids, 1):
        suffix = " [experimental]" if identifier == "nuextract-local" else ""
        output_fn(f"  {index}. [ ] {PLUGIN_METADATA[identifier][0]}{suffix}")
    plugin_indices = _select_indices(
        "Components to install [none]: ",
        default="",
        maximum=len(plugin_ids),
        input_fn=input_fn,
        output_fn=output_fn,
    )
    plugins = tuple(
        identifier
        for index, identifier in enumerate(plugin_ids, 1)
        if index in plugin_indices
    )
    enable_extraction = False
    if "nuextract-local" in plugins:
        output_fn(
            "NuExtract automatic memory writing is experimental; automatic enablement "
            "eval gates are not yet satisfied."
        )
        enable_extraction = _ask_yes_no(
            "Explicitly enable/request automatic extraction policy? [y/N]: ",
            input_fn=input_fn,
            output_fn=output_fn,
        )
    with_mcp = _ask_yes_no(
        # Default yes since task5.md A4: injection alone is proven
        # insufficient — the agent needs tools to QUOTE protected state,
        # not recall it.
        "Register MCP servers too? [Y/n]: ",
        default=True,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    while True:
        scope = input_fn(
            f"Installation scope project/global [{default_scope}]: "
        ).strip().casefold() or default_scope
        if scope in {"project", "global"}:
            break
        output_fn("Invalid scope; enter project or global.")
    if enable_extraction and scope != "project":
        output_fn("Automatic extraction activation requires project scope; request disabled.")
        enable_extraction = False
    return agents, plugins, with_mcp, scope, enable_extraction
