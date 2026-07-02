# Joiny-Mnemonic

Agent-neutral memory core for long-running LLM sessions. The durable source is an immutable,
branch-aware event log; active blocks, typed memories, indexes, summaries and snapshots are
derived views with exact provenance.

## Current status

The core and its local interfaces are implemented and covered by tests. Agent hook installers
generate project-local integrations, but this repository's automated suite does not launch the
four external agent binaries. Semantic retrieval, knowledge graph and KV storage remain plugin
protocols, not built-in implementations.

| Area | Implemented behavior |
|---|---|
| Canonical history | SQLite append-only events/artifacts, hash chain, secret redaction, `synchronous=FULL`, SQL update/delete guards |
| Provenance | Every derived claim references existing events visible in the target branch lineage |
| Protected state | Versioned `instructions`, `goal`, `constraints`, `decisions`, `open_tasks` |
| Retrieval | SQLite FTS5/BM25 candidate retrieval with time/file/type/branch filters and risk/freshness/cost reranking |
| Resume | Materialized snapshot state plus replay tail; protected packet capped at 1500 estimated tokens |
| Snapshots | Recursive JSON patch deltas, cursor, parent/branch lineage, Git/file fingerprints and staleness warnings |
| Transcript safety | Tool calls and outputs are selected atomically; orphan outputs are excluded from resume views |
| Tool-output reduction | Immutable raw output plus command-aware compact/summary views, exact promotion and no-expansion guard |
| Usage observability | Provider-reported samples and labelled local estimates for tokens, cache, cost, latency, bytes and savings |
| Budget governor | Versioned thresholds with rate-limited snapshot, compaction and handoff actions |
| Task boundaries | Task-specific branch, protected goal, snapshots, status history and <=1500-token resume packet |
| Consolidation | Evidence-bound extraction from structured candidates or explicit `Goal:`, `Decision:`, `TODO:` markers |
| Active compaction | Extractive sourced summaries/indexes plus hook-time snapshot and context reinjection |
| Agent integration | Project installers for Claude Code, Codex, OpenCode and OpenHands; idempotent hook receipts and native-session bindings |
| Code context | Live Python AST symbol index, resolved call edges, exact symbol source and reverse impact traversal |
| Evaluation | Evidence-presence diagnostic and a separate external task-runner protocol for real outcome scoring |
| Interfaces | Python, CLI, local HTTP and MCP stdio share one `MemoryService` |

Explicit limits:

- OpenCode resume/compaction uses experimental plugin hooks and may need adjustment after an
  upstream API change.
- The code graph supports Python only. Other languages report unsupported.
- Consolidation is deterministic and evidence-bound; there is no built-in LLM fact extractor.
- Semantic, knowledge-graph and KV features are disabled unless a plugin is installed.
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

## Capture, consolidate and resume

```powershell
joiny-mnemonic append --kind message --role user `
  --content "Goal: ship durable memory`nDecision: use SQLite`nTODO: install hooks"
joiny-mnemonic consolidate
joiny-mnemonic compact --keep-recent 8 --summary-budget 600
joiny-mnemonic snapshot --track README.md
joiny-mnemonic resume --budget 1500 --text-only
```

`consolidate` only promotes explicit evidence. It does not infer unstated facts. Exact promotion
always returns the canonical source:

```powershell
joiny-mnemonic source mem_0123456789abcdef
```

## Install agent hooks

Install the package in the interpreter that the agent can execute, then run one project-local
installer:

```powershell
joiny-mnemonic --project-root . install-hooks codex
joiny-mnemonic --project-root . install-hooks claude-code
joiny-mnemonic --project-root . install-hooks opencode
joiny-mnemonic --project-root . install-hooks openhands
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

joiny-mnemonic code-index
joiny-mnemonic code-search "resume"
joiny-mnemonic code-context "MemoryService.resume"
joiny-mnemonic code-impact "MemoryStore.append_event" --depth 3
```

MCP additionally exposes `memory_code_search`, `memory_code_context` and
`memory_code_impact`, alongside append, derive, search, source, snapshot and resume tools.

## Output reduction, usage, governor and tasks

Raw tool output remains in the hash-chained event log. The prompt assembler may use a smaller
`tool_output_views` representation containing its exact source event ID. Source reads and diffs
pass through unchanged; test/build logs preserve failures and summaries; search output becomes a
complete `path:line` index. A view is rejected when it would be larger than the raw output.

```powershell
joiny-mnemonic output-views evt_0123456789abcdef
joiny-mnemonic source view_0123456789abcdef
joiny-mnemonic usage --branch main

joiny-mnemonic budget-policy --context-window 200000 `
  --snapshot-ratio 0.45 --compact-ratio 0.60 `
  --handoff-ratio 0.75 --hard-limit-ratio 0.90
joiny-mnemonic governor --branch main --apply

joiny-mnemonic task-start ISSUE-417 "Repair invoice accounting"
joiny-mnemonic task-resume ISSUE-417 --budget 1200 --text-only
joiny-mnemonic task-status ISSUE-417 completed --note "Regression test added"
```

The same operations are exposed through HTTP and MCP. Native hooks recognize `task_id`/`taskId`
and bind the complete native session to the task branch.

`UserPromptSubmit` and `PostToolUse` also add their raw, pre-reduction size to an idempotent
per-session cumulative counter. The governor uses the maximum of this counter and any
provider-reported context usage. On crossing `context_window * snapshot_ratio`, the hook creates
or reuses a durable snapshot and injects `[EARLY CONTEXT WARNING]` immediately, including on
`PostToolUse`; it does not wait for `PreCompact`/`PostCompact`.

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

```powershell
joiny-mnemonic serve --host 127.0.0.1 --port 8765
joiny-mnemonic mcp
```

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
evidence-bound consolidation, hook idempotency/installers, Python AST impact, MCP and HTTP.

Architecture: [docs/architecture.md](docs/architecture.md). Security:
[docs/security.md](docs/security.md). Evidence matrix:
[docs/requirements-traceability.md](docs/requirements-traceability.md).
