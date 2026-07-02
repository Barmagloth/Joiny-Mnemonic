# Agent integrations

The hook runtime does three things:

1. binds a native session to one immutable core session;
2. captures hook deliveries with an idempotency receipt;
3. returns a budgeted memory packet through the host's context-injection mechanism.

For `PostToolUse`, the call and output are written in one SQLite transaction. The installer does
not register `PreToolUse`, so resume views cannot observe an output without the matching call.

## Prerequisite

Install `joiny-mnemonic` into a Python interpreter visible to the agent:

```powershell
python -m pip install -e .
```

The generated command uses that exact interpreter (`sys.executable -m joiny_mnemonic`) and absolute
project/database paths.

## Project-local installers

```powershell
joiny-mnemonic --project-root . install-hooks codex
joiny-mnemonic --project-root . install-hooks claude-code
joiny-mnemonic --project-root . install-hooks opencode
joiny-mnemonic --project-root . install-hooks openhands
```

Installers merge their handlers into existing JSON and preserve unrelated hooks. Running the same
installer twice is idempotent.

| Host | Generated file | Capture | Resume injection | Compaction continuity |
|---|---|---|---|---|
| Codex | `.codex/hooks.json` | User prompt, successful tool interaction, assistant stop | `SessionStart`, `UserPromptSubmit` | `PreCompact` snapshot/summary, `PostCompact` reinjection |
| Claude Code | `.claude/settings.json` | User prompt, successful tool interaction, assistant stop | `SessionStart`, `UserPromptSubmit` | `PreCompact` snapshot/summary, `PostCompact` reinjection |
| OpenCode | `.opencode/plugins/joiny-mnemonic.js` | `chat.message`, `tool.execute.after` | `experimental.chat.system.transform` | `experimental.session.compacting` |
| OpenHands | `.openhands/hooks.json` | User prompt, successful tool interaction, assistant stop/session events | `SessionStart`, `UserPromptSubmit` | No joiny-mnemonic compaction hook is installed |

Codex loads project hook configuration only for trusted projects. Its stable command-hook events
and output schemas are documented in the
[Codex hooks reference](https://developers.openai.com/codex/hooks).

Claude Code accepts project hooks in `.claude/settings.json`; `additionalContext` is injected
through `hookSpecificOutput`. See the
[Claude Code hooks reference](https://code.claude.com/docs/en/hooks).

OpenCode's tool hooks are public plugin APIs, but the system-transform and compaction hooks used
for continuity are explicitly experimental. See
[OpenCode plugins](https://opencode.ai/docs/plugins/). Pin and test the OpenCode version used in
production.

OpenHands project hooks use `.openhands/hooks.json` and the Claude-compatible command-hook
contract. See [OpenHands hooks](https://docs.openhands.dev/openhands/usage/customization/hooks).

## Global installation and runtime path resolution

```powershell
joiny-mnemonic install-hooks codex --global
joiny-mnemonic install-hooks claude-code --global
joiny-mnemonic install-hooks opencode --global
```

Global destinations are resolved at install time:

| Host | Resolution order |
|---|---|
| Codex | `$CODEX_HOME/hooks.json`, otherwise `~/.codex/hooks.json` |
| Claude Code | `$CLAUDE_CONFIG_DIR/settings.json`, otherwise `~/.claude/settings.json` |
| OpenCode | `$OPENCODE_CONFIG_DIR/plugins`, `$XDG_CONFIG_HOME/opencode/plugins`, then `~/.config/opencode/plugins` |
| OpenHands | unsupported by the host; use repository `.openhands/hooks.json` |

Re-running `install-hooks` after the rename upgrades generated `python -m llm_memory` command handlers in place. A recognized legacy OpenCode `llm-memory.js` plugin is rewritten as inert before `joiny-mnemonic.js` is installed, preventing duplicate hook delivery.

The installed command uses `hook --global` and contains neither `--db` nor an absolute project
path. Runtime resolution checks explicit project/workspace/cwd fields, then host project
environment variables, then the process cwd, and walks upward to the nearest `.git`. Canonical
state remains project-local at `<resolved-root>/.joiny-mnemonic/memory.db`. If that file does not exist but a legacy `<resolved-root>/.llm-memory/memory.db` does, the runtime reuses the legacy database in place.

## Early raw-context warning

Every unique `UserPromptSubmit` and `PostToolUse` delivery appends one atomic row to
`hook_context_counters`, including the new cumulative total. Tool output is counted before derived-view reduction. Receipts make the
counter retry-safe. The governor compares the cumulative raw estimate with provider-reported
context usage and uses the larger value.

The warning threshold is the branch policy's `context_window_tokens * snapshot_ratio` (defaults:
200,000 * 0.45). At the first crossing, the hook injects `[EARLY CONTEXT WARNING]` even when the
triggering event is `PostToolUse`, and the normal governor path creates a snapshot. This path is
independent of `PreCompact`/`PostCompact`; those remain recovery hooks, not the first detector.

## What is captured

The runtime accepts JSON on stdin and emits JSON only on stdout.

- `SessionStart`: records lifecycle state and injects the resume packet.
- `UserPromptSubmit`: records the prompt, applies evidence-bound consolidation, and injects
  query-relevant memory.
- `PostToolUse`: records one atomic `tool_call` + `tool_output` pair.
- `Stop`: records `last_assistant_message`.
- `PreCompact` / `PostCompact`: consolidate explicit evidence, create an extractive sourced
  summary/index and snapshot, then re-inject restored context where the host supports it.

Secrets are filtered before durable writes. Retrieved history is framed as untrusted data;
protected blocks remain the only instruction-bearing memory.

## Verification

Configuration generation and merge behavior are covered by the repository tests. Host binaries
are external and require a smoke test after installation:

1. start a new session in the target project;
2. submit a prompt containing a unique marker;
3. run one tool successfully;
4. stop or compact the session;
5. inspect `joiny-mnemonic timeline` and `joiny-mnemonic resume --text-only`;
6. confirm exactly one user event and one paired tool interaction;
7. for Codex/Claude/OpenCode, compact and confirm the memory packet appears after compaction.

A generated config is not evidence that a particular host version loaded it. Check the host's
active-hook view/logs as part of deployment.

## MCP is complementary

Hooks provide automatic capture and context injection. MCP provides explicit search/source/code
tools. They may be enabled together using the same database:

```text
python -m joiny_mnemonic --db <project>/.joiny-mnemonic/memory.db \
  --project-root <project> mcp
```

MCP alone does not intercept the transcript.
## Usage, task and governor fields

Hook payloads may include provider `usage` (`input_tokens`, `output_tokens`, cache fields,
`context_tokens`, cost and latency). Values are stored as provider-reported; absent values are not
invented. `task_id`, `taskId`, `task_key` or `taskKey` starts/resolves a task branch on the first
hook and binds subsequent deliveries from the same native session to that task.

After every captured delivery the governor evaluates the branch policy. Snapshot and compaction
are applied before a handoff recommendation is injected. Hook, reduction and usage receipts are
independent, so a retry after a partial crash resumes missing derived work without duplicating
canonical events or metrics.