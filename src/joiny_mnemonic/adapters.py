from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import AgentCapabilities


def _payload_with_call_id(native_event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(native_event)
    for key in ("tool_call_id", "tool_use_id", "call_id", "id"):
        if native_event.get(key) is not None:
            payload["_memory_call_id"] = str(native_event[key])
            break
    return payload


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    kind: str
    content: str
    role: str | None
    payload: dict[str, Any]
    files: tuple[str, ...] = ()


class AgentAdapter(Protocol):
    name: str
    capabilities: AgentCapabilities

    def normalize(self, native_event: dict[str, Any]) -> NormalizedEvent | None: ...


class ClaudeCodeAdapter:
    name = "claude-code"
    capabilities = AgentCapabilities(
        agent=name,
        hooks=frozenset({
            "SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure",
            "Stop", "PreCompact", "PostCompact",
        }),
        kv_access=False,
        lifecycle_events=True,
    )

    def normalize(self, native_event: dict[str, Any]) -> NormalizedEvent | None:
        hook = native_event.get("hook_event_name") or native_event.get("hook")
        if hook == "UserPromptSubmit":
            return NormalizedEvent("message", str(native_event.get("prompt", "")), "user", native_event)
        if hook == "PreToolUse":
            return NormalizedEvent(
                "tool_call", str(native_event.get("tool_name", "tool")), "assistant",
                _payload_with_call_id(native_event),
            )
        if hook in {"PostToolUse", "PostToolUseFailure"}:
            return NormalizedEvent(
                "tool_output", str(native_event.get("tool_response", "")), "tool",
                _payload_with_call_id(native_event),
            )
        if hook in {"SessionStart", "Stop"}:
            return NormalizedEvent("state", hook, None, native_event)
        return None


class CodexAdapter:
    name = "codex"
    capabilities = AgentCapabilities(
        agent=name,
        hooks=frozenset({
            "SessionStart", "UserPromptSubmit", "PostToolUse",
            "Stop", "PreCompact", "PostCompact",
        }),
        kv_access=False,
        lifecycle_events=True,
    )

    def normalize(self, native_event: dict[str, Any]) -> NormalizedEvent | None:
        hook = native_event.get("hook_event_name") or native_event.get("hook")
        if hook == "UserPromptSubmit":
            return NormalizedEvent("message", str(native_event.get("prompt", "")), "user", native_event)
        if hook == "PostToolUse":
            return NormalizedEvent(
                "tool_output", str(native_event.get("tool_response", "")), "tool",
                _payload_with_call_id(native_event),
            )
        if hook == "Stop":
            return NormalizedEvent(
                "message", str(native_event.get("last_assistant_message", "")), "assistant", native_event
            )
        if hook in {"SessionStart", "PreCompact", "PostCompact"}:
            return NormalizedEvent("state", str(hook), None, native_event)
        event_type = str(native_event.get("type", native_event.get("kind", "")))
        role = native_event.get("role")
        content = native_event.get("content", native_event.get("text", ""))
        if event_type in {"message", "user_message", "assistant_message"}:
            if role is None:
                role = "user" if event_type == "user_message" else "assistant"
            return NormalizedEvent("message", str(content), role, native_event)
        if event_type in {"tool_call", "function_call"}:
            return NormalizedEvent(
                "tool_call", str(content or native_event.get("name", "tool")), role,
                _payload_with_call_id(native_event),
            )
        if event_type in {"tool_output", "function_call_output"}:
            return NormalizedEvent(
                "tool_output", str(content or native_event.get("output", "")), "tool",
                _payload_with_call_id(native_event),
            )
        if event_type in {"turn", "turn_started", "turn_completed", "session"}:
            return NormalizedEvent("state", event_type, None, native_event)
        return None


class OpenCodeAdapter:
    name = "opencode"
    capabilities = AgentCapabilities(
        agent=name,
        hooks=frozenset({"chat.message", "tool.execute.after", "experimental.chat.system.transform", "experimental.session.compacting"}),
        kv_access=False,
        lifecycle_events=True,
    )

    def normalize(self, native_event: dict[str, Any]) -> NormalizedEvent | None:
        event_type = str(native_event.get("type", native_event.get("event", ""))).casefold()
        content = native_event.get("content", native_event.get("message", ""))
        files = tuple(str(item) for item in native_event.get("files", ()))
        if event_type in {"message", "user_message", "assistant_message"}:
            return NormalizedEvent(
                "message", str(content), native_event.get("role", "user"), native_event, files
            )
        if event_type in {"action", "tool_call", "run_action"}:
            return NormalizedEvent(
                "tool_call", str(content), "assistant", _payload_with_call_id(native_event), files
            )
        if event_type in {"observation", "tool_output", "action_result"}:
            return NormalizedEvent(
                "tool_output", str(content), "tool", _payload_with_call_id(native_event), files
            )
        if event_type in {"session", "session_start", "session_end"}:
            return NormalizedEvent("state", event_type, None, native_event, files)
        return None


class OpenHandsAdapter(OpenCodeAdapter):
    name = "openhands"
    capabilities = AgentCapabilities(
        agent=name,
        hooks=frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"}),
        kv_access=False,
        lifecycle_events=True,
    )


ADAPTERS: dict[str, AgentAdapter] = {
    adapter.name: adapter
    for adapter in (ClaudeCodeAdapter(), CodexAdapter(), OpenCodeAdapter(), OpenHandsAdapter())
}


def get_adapter(agent: str) -> AgentAdapter | None:
    if agent == "opencode-openhands":
        return ADAPTERS["opencode"]
    return ADAPTERS.get(agent)


def adapter_capabilities(agent: str, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return effective features; absent hooks/KV disable dependent features cleanly."""
    adapter = get_adapter(agent)
    declared = adapter.capabilities if adapter else AgentCapabilities(agent=agent)
    supplied = supplied or {}
    hooks = set(supplied.get("hooks", declared.hooks))
    kv_access = bool(supplied.get("kv_access", declared.kv_access))
    return {
        "agent": agent,
        "event_ingestion": bool(hooks),
        "automatic_resume": bool(hooks & {"SessionStart", "experimental.chat.system.transform"}),
        "tool_capture": bool(hooks & {"PostToolUse", "tool.execute.after"}),
        "tool_failure_capture": "PostToolUseFailure" in hooks,
        "active_compaction": bool(hooks & {"PreCompact", "PostCompact", "experimental.session.compacting"}),
        "hook_installer": agent in {"claude-code", "codex", "opencode", "openhands"},
        "kv_cache_tiers": kv_access,
        "text_memory": True,
        "manual_cli_api_mcp": True,
    }
