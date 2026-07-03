# Joiny-Mnemonic

Agent-neutral memory core for long-running LLM sessions. The durable source is an immutable,
branch-aware event log; active blocks, typed memories, indexes, summaries and snapshots are
derived views with exact provenance.

## Current status

The core and its local interfaces are implemented and covered by tests. Agent hook installers
generate project-local integrations, but this repository's automated suite does not launch the
four external agent binaries. Semantic retrieval and a provenance-aware knowledge graph ship as
separately installable plugins; KV storage remains an extension protocol.

| Area | Implemented behavior |
|---|---|
| Canonical history | SQLite append-only events/artifacts, hash chain, secret redaction, `synchronous=FULL`, SQL update/delete guards |
| Provenance | Every derived claim references existing events visible in the target branch lineage |
| Protected state | Versioned `instructions`, `goal`, `constraints`, `decisions`, `open_tasks` |
| Retrieval | SQLite FTS5/BM25 plus optional local sentence-transformer retrieval over memories and canonical events |
| Resume | Materialized snapshot state plus replay tail; protected packet capped at 1500 estimated tokens |
| Snapshots | Recursive JSON patch deltas, cursor, parent/branch lineage, Git/file fingerprints and staleness warnings |
| Transcript safety | Tool calls and outputs are selected atomically; orphan outputs are excluded from resume views |
| Tool-output reduction | Immutable raw output plus command-aware compact/summary views, exact promotion and no-expansion guard |
| Usage observability | Provider-reported samples and labelled local estimates for tokens, cache, cost, latency, bytes and savings |
| Budget governor | Per-agent JSON profiles with rate-limited snapshot, compaction and handoff actions |
| Task boundaries | Task-specific branch, protected goal, snapshots, status history and <=1500-token resume packet |
| Consolidation | Evidence-bound extraction from structured candidates or explicit `Goal:`, `Decision:`, `Fact:`, `Constraint:`, `TODO:`, `Preference:` markers |
| Active compaction | Extractive sourced summaries/indexes plus hook-time snapshot and context reinjection |
| Agent integration | Project installers for Claude Code, Codex, OpenCode and OpenHands; idempotent hook receipts and native-session bindings |
| Code context | Live Python AST symbol index, resolved call edges, exact symbol source and reverse impact traversal |
| Evaluation | Evidence-presence diagnostic and a separate external task-runner protocol for real outcome scoring |
| Interfaces | Python, CLI, local HTTP and MCP stdio share one `MemoryService` |
| Optional plugins | Persistent local semantic index and provenance-backed SQLite entity graph; both are rebuildable derived views |

Explicit limits:

- OpenCode resume/compaction uses experimental plugin hooks and may need adjustment after an
  upstream API change.
- The code graph supports Python only. Other languages report unsupported.
- Consolidation is deterministic and evidence-bound; there is no built-in LLM fact extractor.
- Semantic and knowledge-graph features are disabled until their separate packages are installed; KV requires a third-party plugin.
- The physical-memory governor selects among supplied candidates; it is not a KV compressor.
- Production readiness still requires host-level integration tests for the exact agent versions
  and a project-specific external task runner.

## Install and inspect

Python 3.11+ is required. There are no mandatory runtime dependencies.

```powershell
python -m pip install -e .
joiny-mnemonic init
joiny-mnemonic capabilities
```

The default database is `.joiny-mnemonic/memory.db`. If only a legacy `.llm-memory/memory.db` exists, Joiny-Mnemonic reuses it in place without a destructive migration. Global options must precede the command:

```powershell
joiny-mnemonic --db .state/memory.db --project-root . verify
```

## Optional semantic search and knowledge graph

The core has no heavy model dependency. Install either plugin from this repository when needed:

```powershell
python -m pip install -e plugins/semantic-local
python -m pip install -e plugins/knowledge-graph
joiny-mnemonic capabilities
```

The semantic plugin indexes both typed memories and ordinary canonical events, so it can retrieve
unmarked prose without keyword overlap. It uses `sentence-transformers/all-MiniLM-L6-v2` by
default and downloads that model on first use; override it with
`JOINY_MNEMONIC_SEMANTIC_MODEL`. The derived vector index is stored under
`.joiny-mnemonic/plugins/semantic.sqlite`.

The graph plugin extracts explicit relations such as
`[[Joiny-Mnemonic]] -[stores]-> [[SQLite]]`, selected natural-language relations between marked
or backticked entities, and lower-confidence co-occurrence edges. Every edge retains its memory
record and canonical source event IDs. Query it through any public interface:

```powershell
joiny-mnemonic graph-neighbors "SQLite" --branch main --limit 20
```

These indexes are derived and rebuildable. Canonical events and typed memories remain the source
of truth.

## Capture, consolidate and resume

```powershell
joiny-mnemonic append --kind message --role user `
  --content "Goal: ship durable memory`nDecision: use SQLite`nFact: WAL is enabled`nTODO: install hooks"
joiny-mnemonic consolidate
joiny-mnemonic compact --keep-recent 8 --summary-budget 600
joiny-mnemonic snapshot --track README.md
joiny-mnemonic resume --budget 1500 --text-only
```

`consolidate` only promotes explicit evidence. It does not infer unstated facts. Every resume packet therefore includes a protected `[DURABLE MEMORY CAPTURE]` instruction: when the agent judges information important across sessions, it should use an available structured memory tool or emit a concise standalone `Goal:`, `Decision:`, `Fact:`, `Constraint:`, `TODO:`, or `Preference:` line. The agent should mark durable, evidence-backed information only, not every message.

Unmarked prose remains immutable and searchable, but it is not guaranteed to enter the compact resume packet automatically. Exact promotion
always returns the canonical source:

```powershell
joiny-mnemonic source mem_0123456789abcdef
```

## Install agent hooks

Install the package in the interpreter that the agent can execute, then run one project-local
installer:

```powershell
joiny-mnemonic --project-root . install-hooks codex --profile gpt-5.2-codex
joiny-mnemonic --project-root . install-hooks claude-code --profile claude-sonnet-4.6
joiny-mnemonic --project-root . install-hooks opencode --profile qwen3-coder
joiny-mnemonic --project-root . install-hooks openhands --profile gpt-5.2-codex
```

For a personal installation across all projects, use `--global`:

```powershell
joiny-mnemonic install-hooks codex --global
joiny-mnemonic install-hooks claude-code --global
joiny-mnemonic install-hooks opencode --global
```

The global hook command contains no install-time project path. At every delivery it resolves the
hook payload's working directory to the nearest Git root and uses that project's
`.joiny-mnemonic/memory.db`. `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `OPENCODE_CONFIG_DIR` and
`XDG_CONFIG_HOME` are honored. OpenHands currently supports repository hooks only, so
`install-hooks openhands --global` fails explicitly.

Installation also writes the selected agent/model limits to
`.joiny-mnemonic/context-limits.json`. Re-running an installer without limit arguments preserves
that agent's existing values. Use `joiny-mnemonic context-profiles` to inspect the seven bundled
model profiles, or pass `--context-window`, `--handoff-tokens`, `--reserve-tokens` and ratio flags
to override a profile. The file is ordinary JSON and may be reviewed or edited directly.

The installers preserve unrelated existing hooks and write:

- `.codex/hooks.json`;
- `.claude/settings.json`;
- `.opencode/plugins/joiny-mnemonic.js`;
- `.openhands/hooks.json`.

Codex project hooks activate only for a trusted project. OpenCode uses
`experimental.chat.system.transform` and `experimental.session.compacting`. Details and host
verification steps are in [docs/integrations.md](docs/integrations.md).

## Retrieval and code context

```powershell
joiny-mnemonic search "snapshot provenance" --type decision --limit 10
joiny-mnemonic timeline --limit 30
joiny-mnemonic graph-neighbors "SQLite" --limit 20

joiny-mnemonic code-index
joiny-mnemonic code-search "resume"
joiny-mnemonic code-context "MemoryService.resume"
joiny-mnemonic code-impact "MemoryStore.append_event" --depth 3
```

MCP additionally exposes `memory_graph_neighbors`, `memory_code_search`, `memory_code_context`
and `memory_code_impact`, alongside append, derive, search, source, snapshot and resume tools.

## Output reduction, usage, governor and tasks

Raw tool output remains in the hash-chained event log. The prompt assembler may use a smaller
`tool_output_views` representation containing its exact source event ID. Source reads and diffs
pass through unchanged; test/build logs preserve failures and summaries; search output becomes a
complete `path:line` index. A view is rejected when it would be larger than the raw output.

```powershell
joiny-mnemonic output-views evt_0123456789abcdef
joiny-mnemonic source view_0123456789abcdef
joiny-mnemonic usage --branch main

joiny-mnemonic context-profiles
joiny-mnemonic budget-policy --agent codex --profile custom `
  --context-window 200000 --handoff-tokens 120000 --reserve-tokens 32000 `
  --snapshot-ratio 0.30 --compact-ratio 0.50 `
  --handoff-ratio 0.70 --hard-limit-ratio 0.90
joiny-mnemonic governor --branch main --agent codex --apply

joiny-mnemonic task-start ISSUE-417 "Repair invoice accounting"
joiny-mnemonic task-resume ISSUE-417 --budget 1200 --text-only
joiny-mnemonic task-status ISSUE-417 completed --note "Regression test added"
```

The same operations are exposed through HTTP and MCP. Native hooks recognize `task_id`/`taskId`
and bind the complete native session to the task branch.

`UserPromptSubmit` and `PostToolUse` also add their raw, pre-reduction size to an idempotent
per-session cumulative counter. The governor uses the maximum of this counter and any
provider-reported context usage. At the snapshot threshold it creates a durable snapshot and
injects a neutral `[CONTEXT CHECKPOINT]`; it does not recommend a new session. The first handoff
recommendation appears only at the configured handoff threshold, and the hard-limit message is
separate. These checks do not wait for `PreCompact`/`PostCompact`.

Advertised context capacity is not a measured quality guarantee. Bundled profiles keep the vendor
window separately from a conservative `recommended_handoff_tokens` cap. The defaults are starting
points, not universal degradation limits; see [docs/context-limits.md](docs/context-limits.md) for
the evidence and calculation.

## Performance and retention benchmark

The benchmark executes real subprocess workloads rather than evaluating hard-coded strings: this
repository's complete test suite, a controlled failing `unittest` suite, `rg` over live source and
`git diff --no-index`. It compares raw and enriched SQLite stores and reports:

- prompt-token delta including view framing;
- reducer and ingest latency distributions;
- SQLite storage amplification;
- critical failure/summary recall, path-reference recall and verbatim-line recall;
- SHA-256 exact-source recovery and promotion latency.

```powershell
python -m pip install -e ".[benchmark]"  # optional exact OpenAI tokenizer
joiny-mnemonic-benchmark --project-root . --repetitions 100 `
  --prompt-exposures 10 --assert-gates
```

Without `tiktoken`, token counts are conservative estimates and the report says so. Provider
usage captured by hooks remains authoritative. Latest checked results are in
[benchmarks/results/latest.md](benchmarks/results/latest.md).

## Evaluation

The legacy diagnostic checks whether required evidence strings survive a policy:

```powershell
joiny-mnemonic evaluate evals/reference_resume_tasks.json --resume-budget 1500
```

It is explicitly not task-level quality. A real task or LLM harness must read one JSON request
from stdin and emit `{ "success": bool, "score": 0..1, "output": string }`:

```powershell
joiny-mnemonic evaluate-runner tasks.json `
  --runner-command '["python","project_eval_runner.py"]' `
  --resume-budget 1500 --minimum 0.95
```

See [docs/evaluation.md](docs/evaluation.md) for the request schema and comparison semantics.

## Local API and MCP

Run the HTTP API or stdio MCP server directly:

```powershell
joiny-mnemonic serve --host 127.0.0.1 --port 8765
joiny-mnemonic --db .joiny-mnemonic/memory.db --project-root . mcp
```

After installing the package, run the client registration command from the project root. Hooks and
MCP may be enabled together against the same database: hooks capture the session automatically;
MCP exposes explicit memory, source, snapshot, resume, semantic search, graph and code-context tools.

```powershell
# Claude Code: current project, private local configuration
claude mcp add --transport stdio --scope local joiny-mnemonic -- `
  joiny-mnemonic --db .joiny-mnemonic/memory.db --project-root . mcp

# Codex CLI and IDE extension
codex mcp add joiny-mnemonic -- `
  joiny-mnemonic --db .joiny-mnemonic/memory.db --project-root . mcp

# OpenHands CLI
openhands mcp add joiny-mnemonic --transport stdio joiny-mnemonic -- `
  --db .joiny-mnemonic/memory.db --project-root . mcp
```

OpenCode provides an interactive installer:

```powershell
opencode mcp add
```

Alternatively, add the server directly to the project's `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "joiny-mnemonic": {
      "type": "local",
      "command": [
        "joiny-mnemonic",
        "--db", ".joiny-mnemonic/memory.db",
        "--project-root", ".",
        "mcp"
      ],
      "enabled": true
    }
  }
}
```

Verify the registered server with the matching client:

```powershell
claude mcp get joiny-mnemonic
codex mcp get joiny-mnemonic
openhands mcp get joiny-mnemonic
opencode mcp list
```

If the client cannot find `joiny-mnemonic` on `PATH`, replace the executable name in the server
command with the absolute path reported by `Get-Command joiny-mnemonic`.

The HTTP server intentionally has no authentication and binds to loopback by default. Put an
authenticating reverse proxy in front of any non-loopback deployment.

## Verification

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

The current suite includes crash durability, immutable storage, branch visibility, recursive
snapshot deltas, restored-state resume, FTS without Python full scans, atomic tool groups,
evidence-bound consolidation, semantic no-keyword retrieval, branch-scoped graph projection,
automatic tamper rejection, hook idempotency/installers, Python AST impact, MCP and HTTP.

Licensed under the [MIT License](LICENSE).

Architecture: [docs/architecture.md](docs/architecture.md). Security:
[docs/security.md](docs/security.md). Evidence matrix:
[docs/requirements-traceability.md](docs/requirements-traceability.md).
