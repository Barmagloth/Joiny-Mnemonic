from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapters import adapter_capabilities
from .consolidation import EvidenceConsolidator
from .context_limits import ContextLimitConfig
from .models import Event
from .paths import resolve_project_database
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


def _hook_events(value: dict[str, Any]) -> list[dict[str, Any]]:
    name = _event_name(value)
    payload = dict(value)
    if name == "UserPromptSubmit":
        return [{"kind": "message", "role": "user", "content": str(value.get("prompt", "")), "payload": payload}]
    if name == "PostToolUse":
        call_id = _call_id(value)
        base = {**payload, "_memory_call_id": call_id}
        return [
            {
                "kind": "tool_call",
                "role": "assistant",
                "content": str(value.get("tool_name", value.get("tool", "tool"))),
                "payload": {**base, "tool_input": value.get("tool_input", value.get("args", {}))},
            },
            {
                "kind": "tool_output",
                "role": "tool",
                "content": _json_text(
                    value.get("tool_response", value.get("tool_output", value.get("output", "")))
                ),
                "payload": base,
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

    receipt_key = _receipt_key(agent, external_session, value)
    events, _created = service.store.append_events_once(
        receipt_key,
        _hook_events(value),
        branch_id=branch_id,
        session_id=session_id,
    )
    service.reduce_tool_outputs(events)
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
    decision = service.governor.evaluate_and_apply(
        branch_id=branch_id,
        session_id=session_id,
        source_event=events[-1],
        agent=agent,
    )

    inject_context = event_name in {"SessionStart", "UserPromptSubmit", "PostCompact"}
    if agent == "opencode" and event_name == "PreCompact":
        inject_context = True
    if warning or any(action in decision.actions for action in ("handoff", "handoff_required")):
        inject_context = True
    if inject_context:
        query = str(value.get("prompt", "resume current goal constraints decisions and open tasks"))
        packet = service.resume(branch_id=branch_id, token_budget=token_budget, query=query)
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
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(data, encoding="utf-8")
    try:
        temporary.replace(path)
    except PermissionError:
        # Some Windows network/project filesystems reject replace-over-existing.
        path.write_text(data, encoding="utf-8")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


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
) -> None:
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
    _write_json(path, config)


def _merge_openhands(path: Path, command: str) -> None:
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
    _write_json(path, config)

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
        path = Path()
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
    if agent == "claude-code":
        if not global_scope:
            assert root is not None
            path = root / ".claude" / "settings.json"
        _merge_command_hooks(path, command, lifecycle_events, nested=True)
        notes = (
            "Global hooks resolve project root from each native hook payload."
            if global_scope else "Project settings are shared with this repository."
        ,)
    elif agent == "codex":
        if not global_scope:
            assert root is not None
            path = root / ".codex" / "hooks.json"
        _merge_command_hooks(path, command, lifecycle_events, nested=True)
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
        path = root / ".openhands" / "hooks.json"
        _merge_openhands(path, command)
        notes = ()
    elif agent == "opencode":
        if not global_scope:
            assert root is not None
            path = root / ".opencode" / "plugins" / "joiny-mnemonic.js"
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
        notes=notes + (
            f"Context profile {limits_policy.profile} is stored in {limits_path}.",
        ),
    )
