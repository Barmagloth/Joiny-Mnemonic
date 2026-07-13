# Agent integrations

The hook runtime does three things:

1. binds a native session to one immutable core session;
2. captures hook deliveries with an idempotency receipt;
3. returns a budgeted memory packet through the host's context-injection mechanism.

For `PostToolUse`, the call and output are written in one SQLite transaction. Claude Code also
registers `PreToolUse`: it records one idempotent state delivery and injects a bounded,
warning-only precheck packet. The packet never heuristically denies the tool call.

## Unified setup

joiny-mnemonic setup is the orchestration layer above the individual adapters. It detects host
products, installs selected optional Python packages, calls the existing idempotent hook
installers, optionally registers MCP, writes .joiny-mnemonic/config.json, and initializes the
project database once. The PowerShell and Bash bootstrap scripts only provision a stable venv and
delegate to this command.

The core selection vocabulary is independent of host brands: repeated --agent and --plugin
arguments describe capabilities, while each adapter owns its host-specific files or CLI call.
--dry-run performs no host/config/database writes. See [installation.md](installation.md).

## Prerequisite

Install `joiny-mnemonic` into a Python interpreter visible to the agent:

```powershell
python -m pip install -e .
```

The generated command uses that exact interpreter (`sys.executable -m joiny_mnemonic`) and absolute
project/database paths.

## Project-local installers

```powershell
joiny-mnemonic --project-root . install-hooks codex --profile gpt-5.2-codex
joiny-mnemonic --project-root . install-hooks claude-code --profile claude-sonnet-4.6
joiny-mnemonic --project-root . install-hooks opencode --profile qwen3-coder
joiny-mnemonic --project-root . install-hooks openhands --profile gpt-5.2-codex
```

Installers merge their handlers into existing JSON and preserve unrelated hooks. Running the same
installer twice is idempotent. Existing JSON is parsed before any installation side effect; a
syntax error names the file, line and column and leaves both the host config and context limits
unchanged. Before replacing a valid JSON config, the installer writes and verifies
`<config>.joiny-mnemonic.bak`. The emitted JSON is parsed after writing; any write/validation
failure restores the original bytes.

| Host | Generated file | Capture | Resume injection | Compaction continuity |
|---|---|---|---|---|
| Codex | `.codex/hooks.json` | User prompt, successful tool interaction, assistant stop | `SessionStart`, `UserPromptSubmit` | `PreCompact` snapshot/summary, `PostCompact` reinjection |
| Claude Code | `.claude/settings.json` | User prompt, pre-action check, successful/failed tool interaction, assistant stop | `SessionStart`, `UserPromptSubmit`, `PreToolUse` warnings | `PreCompact` snapshot/summary, `PostCompact` reinjection |
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

## Context checkpoints and handoff recommendations

Every unique `UserPromptSubmit` and `PostToolUse` delivery appends one atomic row to
`hook_context_counters`, including the new cumulative total. Tool output is counted before
derived-view reduction. Receipts make the counter retry-safe. The governor compares the cumulative
raw estimate with provider-reported context usage and uses the larger value.

The active policy is resolved by agent from `.joiny-mnemonic/context-limits.json`, then from the
global file. A project may therefore run Claude Code and Codex with different context windows and
handoff thresholds on the same branch. The selected model profile and explicit overrides are
written during `install-hooks`; reinstallation without new limit arguments preserves them.

Crossing both the context threshold and the replay-tail byte threshold injects
does not tell the user to start another session. `[CONTEXT HANDOFF RECOMMENDED]` starts only at the
agent's handoff threshold, and `[CONTEXT HANDOFF REQUIRED]` is reserved for the hard limit. This
path is independent of `PreCompact`/`PostCompact`; those remain recovery hooks, not the first
detector. The messages are neutral and event-driven; there is no unconditional branded handoff
instruction in every resume packet.

Advertised windows and operational quality limits are separate. The profile calculation and the
seven bundled model presets are documented in [context-limits.md](context-limits.md).

Every injected resume packet still includes the protected `[DURABLE MEMORY CAPTURE]` instruction.
The agent is told to promote durable, evidence-backed information with an available structured
memory tool or a standalone `Goal:`, `Decision:`, `Fact:`, `Constraint:`, `TODO:`,
`Preference:`, `Failed:`, `Failure:`, or `Lesson:`
marker. Ordinary prose is retained and searchable but is not promised automatic inclusion in
compact resume. Explicit user markers may update protected blocks; assistant markers create
searchable records only. Marker-like text and crafted `memory_candidates` in tool output, state,
artifacts or retrieved memory never change protected memory.
## What is captured

The runtime accepts UTF-8 JSON on stdin and emits JSON only on stdout. A leading UTF-8 BOM is
accepted because native Windows PowerShell pipelines may prefix redirected text with one.

- `SessionStart`: records lifecycle state and injects the resume packet.
- `UserPromptSubmit`: records the prompt, applies evidence-bound consolidation, and injects
  query-relevant memory.
- `PreToolUse` (Claude Code): resolves command/files, runs deterministic precheck, stores the
  redacted report under the idempotent hook receipt and injects at most 4096 UTF-8 bytes. Tool input
  remains untrusted state data and no heuristic finding blocks execution.
- `PostToolUse`: records one atomic `tool_call` + `tool_output` pair.
- `PostToolUseFailure` (Claude Code): records the same atomic pair and derives one concise
  evidence-bound `failure` sourced by both events. It does not infer a lesson or mutate a block.
- `Stop`: records `last_assistant_message`.
- `PreCompact` / `PostCompact`: consolidate explicit evidence, create an extractive sourced
  summary/index and snapshot, then re-inject restored context where the host supports it.

Secrets are filtered before durable writes. Retrieved history is framed as untrusted data;
protected blocks remain the only instruction-bearing memory.

## Explicit Git pre-commit integration

```powershell
joiny-mnemonic --project-root . install-git-hook
```

This command is separate from agent-hook installation. It resolves Git's active hook path,
preserves existing pre-commit content, adds one idempotent Joiny-Mnemonic block, and invokes the
same JSON-producing `precheck --staged` engine. Warning-only reports exit zero. An active
`core.hooksPath` outside the repository is rejected instead of modifying a shared hook directory.

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

Hooks provide automatic capture and context injection. MCP provides explicit search/source/context
and code tools. `memory_source` accepts either the original single `id` or a batch `ids` array;
`memory_context` is the only added P2 tool and returns bounded branch-visible interaction context.
They may be enabled together using the same database:

```text
python -m joiny_mnemonic --db <project>/.joiny-mnemonic/memory.db \
  --project-root <project> mcp
```

MCP alone does not intercept the transcript or persist marker lines merely because they appear in
chat. The initialize response says so on every new MCP connection. For stdio hosts, a relative
`--project-root .` uses the host-provided project environment (`CLAUDE_PROJECT_DIR` in Claude Code),
and relative `--db` paths are anchored to that resolved root. This prevents hooks and MCP from
silently opening two databases when the host launches the MCP process from another directory. When
the client name identifies Claude Code, Codex, OpenCode or OpenHands, it also reports whether
automatic capture is absent, database-split, configured but not yet observed, or observed.

`memory_capabilities` separates installer availability from active state:

- `hook_installer_available`: this host has an installer;
- `hooks_configured`: a generated project/global command was found in valid host config;
- `hook_configuration_status`: `not-configured`, `invalid-config`, `configured`, or
  `configured-with-invalid-config`;
- `hook_database_matches`: the MCP/CLI process opened the project database targeted by hooks;
- `active_database_path` / `hook_expected_database_path`: exact paths for diagnosing split state;
- `hook_runtime_verified`: at least one native hook session reached this database.
- `tool_failure_capture`: true only when the adapter/host exposes and installs
  `PostToolUseFailure`; unsupported hosts remain false.
- `pre_action_precheck`: true only when the host installer configures `PreToolUse`.

Until valid configuration is detected and a hook delivery is observed, automatic
ingestion/resume/tool-capture capabilities remain false and the response includes an explicit
install, repair, or verification warning.
## Usage, task and governor fields

Hook payloads may include provider `usage` (`input_tokens`, `output_tokens`, cache fields,
`context_tokens`, cost and latency). Values are stored as provider-reported; absent values are not
invented. `task_id`, `taskId`, `task_key` or `taskKey` starts/resolves a task branch on the first
hook and binds subsequent deliveries from the same native session to that task.

After every captured delivery the governor evaluates the branch policy. Snapshot and compaction
are applied before a handoff recommendation is injected. Hook, reduction and usage receipts are
independent, so a retry after a partial crash resumes missing derived work without duplicating
canonical events or metrics. Prompt-injection exposure uses its own receipt, so repeated native
delivery does not double-count it.

## Optional automatic extraction

Automatic extraction is an optional plugin category named joiny_mnemonic.extractor. Core has no
ML dependency and the kill switch is off by default. The bundled optional NuExtract package is
under plugins/nuextract-local and imports Transformers/Torch only inside the plugin.

Set JOINY_MNEMONIC_EXTRACTOR_ENABLED=1 only after installing a backend and validating its pinned
Canonical append emits only a durable coalescible wakeup. Persistent MCP/HTTP services use a
bounded background consumer; one-shot hooks launch a detached worker that claims the same
expiring database lease. extraction-process is the explicit foreground recovery/drain command.

All MCP and HTTP append calls are stamped public_api regardless of a claimed role or provenance.
Only events delivered through an installed host hook are stamped host_hook, so a public
role=user append cannot confirm a candidate or modify protected blocks.

configuration. Useful operations are extraction-status, extraction-process, extraction-retry,
extraction-reprocess and extraction-candidates. HTTP exposes corresponding /v1/extraction
routes. MCP exposes extraction status/process and provenance-bound candidate requests.

CLI, HTTP, MCP and tool calls are not trusted human approval. Candidate confirmation, rejection
or supersession calls therefore append a canonical control event and a requested transition.
A trusted explicit user marker can confirm an exact normalized type/content match.

Capabilities report availability, enablement, backend/hash, pending and failed events, oldest
backlog age, retries, quarantine age, witness state and security findings. Queue pressure changes
latency, never canonical capture.
