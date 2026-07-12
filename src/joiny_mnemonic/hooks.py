from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapters import adapter_capabilities
from .context_limits import ContextLimitConfig
from .paths import resolve_project_database
from .reducers import first_failure_line
from .service import MemoryService


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _event_name(value: dict[str, Any]) -> str:
    return str(
        value.get("hook_event_name")
        or value.get("hookEventName")
        or value.get("event_type")
        or value.get("type")
        or value.get("event")
        or "event"
    )


def _call_id(value: dict[str, Any]) -> str:
    for key in ("tool_use_id", "tool_call_id", "callID", "call_id", "id"):
        if value.get(key) is not None:
            return str(value[key])
    digest = hashlib.sha256(_json_text(value.get("tool_input", value)).encode()).hexdigest()
    return digest[:24]


def _receipt_key(agent: str, external_session: str, value: dict[str, Any]) -> str:
    transcript_version: tuple[int, int] | None = None
    transcript_path = value.get("transcript_path")
    if transcript_path:
        try:
            stat = Path(str(transcript_path)).stat()
            transcript_version = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            pass
    identity = {
        "agent": agent,
        "session": external_session,
        "event": _event_name(value),
        "turn": value.get("turn_id", value.get("turnID")),
        "call": _call_id(value) if "ToolUse" in _event_name(value) else None,
        "message": value.get("message_id", value.get("messageID")),
        "content": value.get("prompt", value.get("last_assistant_message")),
        "transcript_version": transcript_version,
    }
    return "hook:" + hashlib.sha256(_json_text(identity).encode()).hexdigest()


def _native_session(value: dict[str, Any]) -> str:
    for key in ("session_id", "sessionID", "conversation_id", "conversationID"):
        if value.get(key):
            return str(value[key])
    transcript = value.get("transcript_path")
    if transcript:
        return "transcript:" + hashlib.sha256(str(transcript).encode()).hexdigest()[:24]
    return "project-default"


def resolve_hook_project(
    value: dict[str, Any], *, fallback: str | Path | None = None
) -> Path:
    """Resolve the current project at hook runtime instead of install time."""
    candidates: list[Any] = [
        value.get("project_root"), value.get("projectRoot"),
        value.get("workspace_root"), value.get("workspaceRoot"),
        value.get("working_dir"), value.get("workingDir"), value.get("cwd"),
        os.environ.get("CLAUDE_PROJECT_DIR"), os.environ.get("OPENHANDS_PROJECT_DIR"),
        fallback, Path.cwd(),
    ]
    start: Path | None = None
    for candidate in candidates:
        if not candidate:
            continue
        expanded = os.path.expandvars(os.path.expanduser(str(candidate)))
        path = Path(expanded)
        try:
            path = path.resolve()
        except OSError:
            continue
        if path.is_file():
            path = path.parent
        if path.is_dir():
            start = path
            break
    if start is None:
        raise ValueError("cannot resolve project root from hook payload")
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def resolve_global_install_path(
    agent: str,
    *,
    environ: dict[str, str] | None = None,
    home: str | Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    user_home = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()

    def configured(name: str) -> Path | None:
        value = env.get(name)
        if not value:
            return None
        return Path(os.path.expandvars(os.path.expanduser(value))).resolve()

    if agent == "claude-code":
        root = configured("CLAUDE_CONFIG_DIR") or user_home / ".claude"
        return root / "settings.json"
    if agent == "codex":
        root = configured("CODEX_HOME") or user_home / ".codex"
        return root / "hooks.json"
    if agent == "opencode":
        root = configured("OPENCODE_CONFIG_DIR")
        if root is None:
            xdg = configured("XDG_CONFIG_HOME")
            root = (xdg / "opencode") if xdg is not None else user_home / ".config" / "opencode"
        return root / "plugins" / "joiny-mnemonic.js"
    if agent == "openhands":
        raise ValueError(
            "OpenHands does not load user-global hooks; install repository-local "
            ".openhands/hooks.json without --global"
        )
    raise ValueError(f"unsupported hook installer: {agent}")


def resolve_project_install_path(agent: str, project_root: str | Path) -> Path:
    root = Path(project_root).expanduser().resolve()
    if agent == "claude-code":
        return root / ".claude" / "settings.json"
    if agent == "codex":
        return root / ".codex" / "hooks.json"
    if agent == "opencode":
        return root / ".opencode" / "plugins" / "joiny-mnemonic.js"
    if agent == "openhands":
        return root / ".openhands" / "hooks.json"
    raise ValueError(f"unsupported hook installer: {agent}")

def _task_key(value: dict[str, Any]) -> str | None:
    for key in ("task_id", "taskId", "task_key", "taskKey"):
        if value.get(key):
            return str(value[key])
    task = value.get("task")
    if isinstance(task, dict):
        for key in ("id", "key", "task_id"):
            if task.get(key):
                return str(task[key])
    return None


def _tool_files(value: dict[str, Any]) -> tuple[str, ...]:
    found: list[str] = []

    def add(candidate: Any) -> None:
        values = candidate if isinstance(candidate, (list, tuple, set)) else (candidate,)
        for item in values:
            if item is None:
                continue
            path = str(item).strip()
            if path and path not in found:
                found.append(path)

    inputs = value.get("tool_input", value.get("args", {}))
    for container in (value, inputs if isinstance(inputs, dict) else {}):
        for key in ("file_path", "path", "paths", "filename", "files"):
            if key in container:
                add(container[key])
    return tuple(found)


def _first_nonempty_line(value: Any) -> str | None:
    text = _json_text(value) if not isinstance(value, str) else value
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = " ".join(raw.split())
        if line:
            return line[:240]
    return None


def _explicit_failure_line(value: dict[str, Any]) -> str | None:
    containers: list[dict[str, Any]] = [value]
    for key in ("tool_response", "tool_output", "output"):
        nested = value.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for key in ("error", "message"):
            candidate = container.get(key)
            if isinstance(candidate, dict):
                nested = _explicit_failure_line(candidate)
                if nested:
                    return nested
            elif candidate is not None:
                line = _first_nonempty_line(candidate)
                if line:
                    return line
    return None


def _tool_command(value: dict[str, Any]) -> str | None:
    inputs = value.get("tool_input", value.get("args", {}))
    containers = (value, inputs if isinstance(inputs, dict) else {})
    for container in containers:
        for key in ("command", "cmd", "script", "query"):
            if container.get(key):
                return str(container[key])
    return str(inputs) if isinstance(inputs, str) and inputs.strip() else None


def _derive_native_failure(
    service: MemoryService,
    value: dict[str, Any],
    events: tuple[Any, ...],
) -> None:
    pair = tuple(event for event in events if event.kind in {"tool_call", "tool_output"})
    if len(pair) != 2:
        return
    source_ids = tuple(event.id for event in pair)
    output = next(event for event in pair if event.kind == "tool_output")
    tool_name = str(
        value.get("tool_name", value.get("tool", value.get("name", pair[0].content or "tool")))
    ).strip() or "tool"
    detail = (
        _explicit_failure_line(value)
        or first_failure_line(output.content)
        or _first_nonempty_line(output.content)
    )
    content = f"{tool_name} failed" + (f": {detail}" if detail else "")
    files = tuple(dict.fromkeys((*_tool_files(value), *(path for event in pair for path in event.files))))
    for record in service.store.list_memories(
        branch_id=pair[0].branch_id, include_superseded=True
    ):
        if (
            record.memory_type == "failure"
            and record.source_event_ids == source_ids
            and record.content == content
        ):
            return
    service.derive_memory(
        memory_type="failure",
        content=content,
        source_event_ids=source_ids,
        files=files,
        branch_id=pair[0].branch_id,
    )


def _hook_events(value: dict[str, Any]) -> list[dict[str, Any]]:
    name = _event_name(value)
    payload = dict(value)
    if name == "UserPromptSubmit":
        return [{"kind": "message", "role": "user", "content": str(value.get("prompt", "")), "payload": payload}]
    if name in {"PostToolUse", "PostToolUseFailure"}:
        call_id = _call_id(value)
        base = {**payload, "_memory_call_id": call_id}
        files = _tool_files(value)
        return [
            {
                "kind": "tool_call",
                "role": "assistant",
                "content": str(value.get("tool_name", value.get("tool", "tool"))),
                "payload": {**base, "tool_input": value.get("tool_input", value.get("args", {}))},
                "files": files,
            },
            {
                "kind": "tool_output",
                "role": "tool",
                "content": _json_text(
                    value.get("tool_response", value.get("tool_output", value.get("output", "")))
                ),
                "payload": base,
                "files": files,
            },
        ]
    if name == "Stop":
        return [
            {
                "kind": "message",
                "role": "assistant",
                "content": str(value.get("last_assistant_message", value.get("message", ""))),
                "payload": payload,
            }
        ]
    return [{"kind": "state", "role": None, "content": name, "payload": payload}]


def _context_output(agent: str, event_name: str, context: str) -> dict[str, Any]:
    if not context:
        return {}
    if agent in {"codex", "claude-code"}:
        return {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            }
        }
    return {"additionalContext": context}


def process_hook(
    service: MemoryService,
    agent: str,
    value: dict[str, Any],
    *,
    branch_id: str = "main",
    token_budget: int = 1500,
) -> dict[str, Any]:
    """Capture one native hook delivery and return the agent's context-injection JSON."""
    event_name = _event_name(value)
    external_session = _native_session(value)
    task_key = _task_key(value)
    task = None
    if task_key is not None:
        task = service.tasks.ensure(
            task_key,
            title=str(value.get("task_title", value.get("taskTitle", task_key))),
            parent_branch=branch_id,
            metadata={"agent": agent, "external_session": external_session},
        )
    else:
        task = service.store.task_for_hook_session(agent, external_session)
    if task is not None:
        branch_id = task.branch_id

    session_id = service.store.hook_session(
        agent,
        external_session,
        branch_id=branch_id,
        capabilities=adapter_capabilities(agent),
    )
    if task is not None:
        service.store.bind_task_session(session_id, task.task_key)

    if event_name in {"PreCompact", "PostCompact"}:
        service.consolidator.consolidate_pending(service, branch_id=branch_id)
        service.consolidator.compact(service, branch_id=branch_id)
        service.create_snapshot(branch_id=branch_id)

    precheck_report = None
    capture_value = value
    if event_name == "PreToolUse":
        precheck_report = service.precheck(
            files=_tool_files(value),
            command=_tool_command(value),
            branch_id=branch_id,
        )
        capture_value = {
            **value,
            "_joiny_precheck": asdict(precheck_report),
        }

    receipt_key = _receipt_key(agent, external_session, value)
    events, _created = service.store.append_events_once(
        receipt_key,
        _hook_events(capture_value),
        branch_id=branch_id,
        session_id=session_id,
    )
    service.reduce_tool_outputs(events)
    if event_name == "PostToolUseFailure":
        _derive_native_failure(service, value, events)
    service.usage.capture_native(
        value,
        source=agent,
        branch_id=branch_id,
        session_id=session_id,
        event_id=events[-1].id,
        receipt_key=f"usage:{receipt_key}",
    )
    policy = service.budget_policy(branch_id=branch_id, agent=agent)
    counter = service.usage.record_hook_context(
        events,
        event_name=event_name,
        branch_id=branch_id,
        session_id=session_id,
        receipt_key=receipt_key,
        context_window_tokens=policy.context_window_tokens,
        threshold_tokens=service.governor.thresholds(policy)["snapshot"],
    )
    warning = (
        service.governor.register_context_checkpoint(
            counter,
            branch_id=branch_id,
            session_id=session_id,
            source_event=events[-1],
            agent=agent,
        )
        if counter is not None else False
    )
    # A retry may follow a crash after capture but before consolidation; receipts make
    # capture idempotent and every derived subsystem has its own idempotent receipt.
    service.consolidator.consolidate_pending(service, branch_id=branch_id, events=events)
    if service.extraction.enabled:
        service.extraction.process_backlog()
    service.checkpoint_witness()
    decision = service.governor.evaluate_and_apply(
        branch_id=branch_id,
        session_id=session_id,
        source_event=events[-1],
        agent=agent,
    )

    if event_name == "PreToolUse":
        stored = events[-1].payload.get("_joiny_precheck")
        if isinstance(stored, dict):
            precheck_report = service.prechecks.from_dict(stored)
        if precheck_report is not None:
            warning_packet = service.prechecks.render(precheck_report, max_bytes=4096)
            if warning_packet:
                return _context_output(agent, event_name, warning_packet)

    inject_context = event_name in {"SessionStart", "UserPromptSubmit", "PostCompact"}
    if agent == "opencode" and event_name == "PreCompact":
        inject_context = True
    if warning or any(action in decision.actions for action in ("handoff", "handoff_required")):
        inject_context = True
    if inject_context:
        query = str(value.get("prompt", "resume current goal constraints decisions and open tasks"))
        packet = service.resume(
            branch_id=branch_id,
            token_budget=token_budget,
            query=query,
            session_id=session_id,
            task_key=task.task_key if task is not None else None,
            telemetry_receipt=f"prompt-injection:{receipt_key}",
        )
        context = packet.text
        thresholds = service.governor.thresholds(policy)
        if warning and counter is not None:
            context += (
                "\n\n[CONTEXT CHECKPOINT]\n"
                f"Cumulative raw UserPromptSubmit/PostToolUse context is approximately "
                f"{counter.cumulative_tokens}/{counter.context_window_tokens} tokens "
                f"({counter.ratio:.1%}). A durable snapshot was captured before native "
                f"compaction. Handoff is not recommended until approximately "
                f"{thresholds['handoff']} tokens."
            )
        if "handoff_required" in decision.actions:
            context += (
                "\n\n[CONTEXT HANDOFF REQUIRED]\n"
                f"Context usage is approximately {decision.context_tokens}/"
                f"{policy.context_window_tokens} tokens ({decision.context_ratio:.1%}). "
                "Start a new session now to avoid lossy native compaction; a durable resume "
                "packet is available."
            )
        elif "handoff" in decision.actions:
            context += (
                "\n\n[CONTEXT HANDOFF RECOMMENDED]\n"
                f"Context usage is approximately {decision.context_tokens}/"
                f"{policy.context_window_tokens} tokens ({decision.context_ratio:.1%}). "
                "Consider starting a new session for this task; a durable resume packet is "
                "available."
            )
        return _context_output(agent, event_name, context)
    return {}

@dataclass(frozen=True, slots=True)
class InstallResult:
    agent: str
    files: tuple[str, ...]
    command: str
    status: str
    scope: str = "project"
    profile: str | None = None
    limits_file: str | None = None
    backup_file: str | None = None
    notes: tuple[str, ...] = ()


def _command(
    project_root: Path | None,
    agent: str,
    branch_id: str,
    token_budget: int,
    *,
    global_scope: bool = False,
) -> str:
    args = [sys.executable, "-m", "joiny_mnemonic"]
    if not global_scope:
        if project_root is None:
            raise ValueError("project root is required for project-local hooks")
        args.extend(
            [
                "--db", str(resolve_project_database(project_root)),
                "--project-root", str(project_root),
            ]
        )
    args.extend(
        [
            "hook", "--agent", agent, "--branch", branch_id,
            "--budget", str(token_budget),
        ]
    )
    if global_scope:
        args.append("--global")
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path} is not valid UTF-8 JSON; file was not modified"
        ) from exc
    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} contains invalid JSON at line {exc.lineno}, "
            f"column {exc.colno}; file was not modified"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object; file was not modified")
    return value


def _durable_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def install_git_precommit(project_root: str | Path) -> dict[str, str]:
    root = Path(project_root).resolve()
    def git_path(*arguments: str) -> Path:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", *arguments],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(f"{root} is not a Git repository")
        value = Path(completed.stdout.strip())
        return value.resolve() if value.is_absolute() else (root / value).resolve()

    common_dir = git_path("--git-common-dir")
    hooks_dir = git_path("--git-path", "hooks")
    allowed_roots = (root, common_dir)
    if not any(
        hooks_dir == allowed or allowed in hooks_dir.parents
        for allowed in allowed_roots
    ):
        raise ValueError(
            "active core.hooksPath is outside this repository; configure a "
            "repository-local hooks path before installing"
        )
    path = hooks_dir / "pre-commit"
    begin = "# joiny-mnemonic precheck begin"
    end = "# joiny-mnemonic precheck end"
    command = shlex.join(
        [
            sys.executable,
            "-m",
            "joiny_mnemonic",
            "--db",
            str(resolve_project_database(root)),
            "--project-root",
            str(root),
            "precheck",
            "--staged",
        ]
    )
    block = f"{begin}\n{command}\n{end}"
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path} is not UTF-8; hook was not modified") from exc
    else:
        existing = "#!/bin/sh\n"
    if begin in existing:
        start = existing.index(begin)
        finish = existing.find(end, start)
        if finish < 0:
            raise ValueError(f"{path} contains an incomplete joiny-mnemonic block")
        finish += len(end)
        updated = existing[:start] + block + existing[finish:]
        status = "updated"
    else:
        updated = existing.rstrip() + "\n\n" + block + "\n"
        status = "installed"
    _durable_write(path, updated.encode("utf-8"))
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass
    return {"path": str(path), "status": status, "command": command}


def _json_backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".joiny-mnemonic.bak")

def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_json(path: Path, value: dict[str, Any]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    rendered = json.loads(data)
    if not isinstance(rendered, dict):
        raise ValueError("serialized hook configuration must be a JSON object")

    original = path.read_bytes() if path.exists() else None
    backup: Path | None = None
    if original is not None:
        _read_json(path)
        backup = _json_backup_path(path)
        backup_temporary = backup.with_suffix(backup.suffix + ".tmp")
        _durable_write(backup_temporary, original)
        try:
            backup_temporary.replace(backup)
        except PermissionError:
            _durable_write(backup, original)
            _safe_unlink(backup_temporary)
        if backup.read_bytes() != original:
            raise OSError(f"failed to verify backup for {path}")

    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        _durable_write(temporary, data)
        try:
            temporary.replace(path)
        except PermissionError:
            # Network filesystems may reject replace-over-existing. The verified
            # backup makes this fallback recoverable.
            _durable_write(path, data)
            _safe_unlink(temporary)
        _read_json(path)
    except Exception:
        try:
            if original is None:
                _safe_unlink(path)
            else:
                _durable_write(path, original)
                _read_json(path)
        except Exception as restore_exc:
            raise RuntimeError(
                f"failed to write {path} and failed to restore its verified backup"
            ) from restore_exc
        raise
    finally:
        _safe_unlink(temporary)
    return backup


def _contains_hook_command(value: Any, agent: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_hook_command(item, agent) for item in value.values())
    if isinstance(value, list):
        return any(_contains_hook_command(item, agent) for item in value)
    if not isinstance(value, str):
        return False
    folded = value.casefold()
    return (
        ("joiny_mnemonic" in folded or "joiny-mnemonic" in folded)
        and "hook" in folded
        and agent.casefold() in folded
    )


def hook_installation_status(
    project_root: str | Path,
    agent: str,
    *,
    environ: dict[str, str] | None = None,
    home: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    candidates: list[tuple[str, Path]] = [
        ("project", resolve_project_install_path(agent, root))
    ]
    if agent != "openhands":
        candidates.append(
            (
                "global",
                resolve_global_install_path(agent, environ=environ, home=home),
            )
        )

    checked: list[str] = []
    configured: list[str] = []
    configured_scopes: list[str] = []
    invalid: list[dict[str, str]] = []
    seen: set[Path] = set()
    for scope, path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        checked.append(str(resolved))
        if not resolved.exists():
            continue
        try:
            if agent == "opencode":
                value: Any = resolved.read_text(encoding="utf-8")
            else:
                value = _read_json(resolved)
        except (OSError, ValueError) as exc:
            invalid.append({"path": str(resolved), "error": str(exc)})
            continue
        if _contains_hook_command(value, agent):
            configured.append(str(resolved))
            configured_scopes.append(scope)

    is_configured = bool(configured)
    if is_configured and invalid:
        status = "configured-with-invalid-config"
    elif is_configured:
        status = "configured"
    elif invalid:
        status = "invalid-config"
    else:
        status = "not-configured"
    command = f'joiny-mnemonic --project-root "{root}" install-hooks {agent}'
    return {
        "status": status,
        "configured": is_configured,
        "config_valid": not invalid,
        "checked_paths": checked,
        "configured_paths": configured,
        "configured_scopes": configured_scopes,
        "invalid_configs": invalid,
        "install_command": command,
    }

def _upsert_hook_command(
    groups: list[Any], command: str, *, typed: bool
) -> bool:
    """Replace generated pre-rename commands and avoid duplicate hook delivery."""
    for group in groups:
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks", ())
        if not isinstance(handlers, list):
            continue
        for handler in handlers:
            if not isinstance(handler, dict):
                continue
            existing = handler.get("command")
            if existing == command:
                return True
            if isinstance(existing, str) and re.search(
                r"(?:^|\s)-m\s+[\"']?llm_memory(?:[\"']?\s|$)", existing
            ):
                handler["command"] = command
                handler["timeout"] = 30
                if typed:
                    handler["type"] = "command"
                return True
    return False


def _merge_command_hooks(
    path: Path,
    command: str,
    events: dict[str, str | None],
    *,
    nested: bool,
) -> Path | None:
    config = _read_json(path)
    hooks = config.setdefault("hooks", {}) if nested else config
    if not isinstance(hooks, dict):
        raise ValueError(f"hooks in {path} must be an object")
    for event, matcher in events.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"hook event {event} in {path} must be an array")
        if _upsert_hook_command(groups, command, typed=True):
            continue
        handler: dict[str, Any] = {"type": "command", "command": command, "timeout": 30}
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
    return _write_json(path, config)


def _merge_openhands(path: Path, command: str) -> Path | None:
    config = _read_json(path)
    for event, matcher in {
        "session_start": "*",
        "user_prompt_submit": None,
        "post_tool_use": "*",
        "stop": "*",
        "session_end": "*",
    }.items():
        groups = config.setdefault(event, [])
        if _upsert_hook_command(groups, command, typed=False):
            continue
        handler = {"command": command, "timeout": 30}
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
    return _write_json(path, config)

def _opencode_plugin(command: str) -> str:
    encoded = json.dumps(command)
    return f'''import {{ spawnSync }} from "node:child_process"

const command = {encoded}
const seen = new Set()

function run(input) {{
  const result = spawnSync(command, {{ input: JSON.stringify(input), shell: true, encoding: "utf8" }})
  if (result.status !== 0) throw new Error(result.stderr || `joiny-mnemonic hook failed: ${{result.status}}`)
  return result.stdout.trim() ? JSON.parse(result.stdout) : {{}}
}}

export const JoinyMnemonicPlugin = async ({{ directory }}) => ({{
  "chat.message": async (input, output) => {{
    const prompt = (output.parts ?? [])
      .map((part) => part.text ?? part.content ?? JSON.stringify(part))
      .join("\\n")
    run({{ ...input, messageID: input.messageID ?? output.message?.id, hook_event_name: "UserPromptSubmit", prompt, cwd: directory }})
  }},
  "tool.execute.after": async (input, output) => {{
    run({{ ...input, hook_event_name: "PostToolUse", tool_name: input.tool, tool_input: input.args, tool_response: output, cwd: directory }})
  }},
  "experimental.chat.system.transform": async (input, output) => {{
    const sessionID = input.sessionID ?? "project-default"
    if (seen.has(sessionID)) return
    seen.add(sessionID)
    const value = run({{ ...input, sessionID, hook_event_name: "SessionStart", cwd: directory }})
    const context = value.additionalContext ?? value.hookSpecificOutput?.additionalContext
    if (context) output.system.push(context)
  }},
  "experimental.session.compacting": async (input, output) => {{
    const value = run({{ ...input, hook_event_name: "PreCompact", cwd: directory }})
    const context = value.additionalContext ?? value.hookSpecificOutput?.additionalContext
    if (context) output.context.push(context)
  }},
}})
'''


def install_hooks(
    agent: str,
    project_root: str | Path | None = None,
    *,
    branch_id: str = "main",
    token_budget: int = 1500,
    global_scope: bool = False,
    profile: str | None = None,
    context_window_tokens: int | None = None,
    snapshot_ratio: float | None = None,
    compact_ratio: float | None = None,
    handoff_ratio: float | None = None,
    hard_limit_ratio: float | None = None,
    recommended_handoff_tokens: int | None = None,
    reserve_tokens: int | None = None,
    min_action_interval_events: int | None = None,
    environ: dict[str, str] | None = None,
    home: str | Path | None = None,
) -> InstallResult:
    root: Path | None
    if global_scope:
        root = None
        path = resolve_global_install_path(agent, environ=environ, home=home)
    else:
        root = Path(project_root or ".").resolve()
        if not root.is_dir():
            raise FileNotFoundError(root)
        path = resolve_project_install_path(agent, root)

    # Validate host-owned JSON before writing the limits file or any hook config.
    if agent in {"claude-code", "codex", "openhands"}:
        _read_json(path)

    limits = ContextLimitConfig(root or Path.cwd(), environ=environ, home=home)
    limits_path, limits_policy = limits.configure_agent(
        agent,
        profile=profile,
        global_scope=global_scope,
        overrides={
            "context_window_tokens": context_window_tokens,
            "snapshot_ratio": snapshot_ratio,
            "compact_ratio": compact_ratio,
            "handoff_ratio": handoff_ratio,
            "hard_limit_ratio": hard_limit_ratio,
            "recommended_handoff_tokens": recommended_handoff_tokens,
            "reserve_tokens": reserve_tokens,
            "min_action_interval_events": min_action_interval_events,
        },
    )
    command = _command(
        root,
        agent,
        branch_id,
        min(token_budget, 1500),
        global_scope=global_scope,
    )
    lifecycle_events = {
        "SessionStart": "startup|resume|clear|compact",
        "UserPromptSubmit": None,
        "PostToolUse": "*",
        "Stop": None,
        "PreCompact": "manual|auto",
        "PostCompact": "manual|auto",
    }
    backup_path: Path | None = None
    if agent == "claude-code":
        backup_path = _merge_command_hooks(
            path,
            command,
            {
                **lifecycle_events,
                "PreToolUse": "*",
                "PostToolUseFailure": "*",
            },
            nested=True,
        )
        notes = (
            "Global hooks resolve project root from each native hook payload."
            if global_scope else "Project settings are shared with this repository."
        ,)
    elif agent == "codex":
        backup_path = _merge_command_hooks(
            path, command, lifecycle_events, nested=True
        )
        notes = (
            "User-level Codex hooks load independently of project trust and resolve each project at runtime.",
        ) if global_scope else (
            "Project-local Codex hooks run only after the project is trusted.",
        )
    elif agent == "openhands":
        if global_scope:
            # resolve_global_install_path already rejects this; keep the guard explicit.
            raise ValueError("OpenHands does not support user-global hook files")
        assert root is not None
        backup_path = _merge_openhands(path, command)
        notes = ()
    elif agent == "opencode":
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path = path.with_name("llm-memory.js")
        if legacy_path.exists():
            legacy_source = legacy_path.read_text(encoding="utf-8", errors="replace")
            if "LlmMemoryPlugin" in legacy_source or "-m llm_memory" in legacy_source:
                legacy_path.write_text(
                    "// Migrated to joiny-mnemonic.js; intentionally inert.\n",
                    encoding="utf-8",
                )
        path.write_text(_opencode_plugin(command), encoding="utf-8")
        notes = (
            "OpenCode resume injection uses experimental.chat.system.transform.",
            "OpenCode compaction injection uses experimental.session.compacting.",
            "Global plugin resolves each project from the plugin directory payload."
            if global_scope else "Project plugin uses the configured project root.",
        )
    else:
        raise ValueError(f"unsupported hook installer: {agent}")
    return InstallResult(
        agent=agent,
        files=(str(path), str(limits_path)),
        command=command,
        status="installed",
        scope="global" if global_scope else "project",
        profile=limits_policy.profile,
        limits_file=str(limits_path),
        backup_file=str(backup_path) if backup_path is not None else None,
        notes=notes + (
            f"Context profile {limits_policy.profile} is stored in {limits_path}.",
        ),
    )
