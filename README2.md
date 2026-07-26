# Joiny-Mnemonic

**A provenance-first history ledger for long-running AI work.**

Joiny-Mnemonic keeps the history an agent may later summarize, retrieve, or act around without
letting those derived representations silently replace what actually happened.

The durable core is an append-only SQLite event log. Memories, active state, summaries, search
indexes, snapshots, and prompt packets are projections over that log. Every promoted claim can be
traced back to exact source events.

Joiny can be the memory core for a new agent, but it does not have to replace an existing memory
system. It can also run beside one as the canonical history, provenance, temporal, and audit layer.

## The problem

Long-running agents do not fail only because they forget. They also fail because compressed
context quietly changes meaning:

- a summary replaces the source it summarized;
- a newer fact overwrites an older fact without preserving when either was valid;
- a restored snapshot continues as if the abandoned future never existed;
- an old task or instruction reappears without its author, evidence, scope, or current status;
- a reduced tool result loses the exact output needed to audit a later conclusion.

Retrieval alone does not solve these problems. Better search can retrieve the wrong revision more
confidently. Joiny starts one layer below retrieval: with a history that remains inspectable after
compression, correction, rollback, and branching.

## What Joiny guarantees

### The source is not the summary

Canonical events and artifacts are append-only. SQL guards reject their update or deletion. A
summary, extracted fact, compact tool-output view, snapshot, or index points back to its source; it
does not become a replacement source of truth.

An agent can begin with a cheap representation and promote it to the unchanged original when the
task needs exact evidence:

```powershell
joiny-mnemonic search "why did the migration fail?"
joiny-mnemonic source mem_0123456789abcdef
joiny-mnemonic context mem_0123456789abcdef --before 3 --after 3 --include-source
```

### Corrections preserve time

Joiny separates two questions that ordinary memory stores often conflate:

- **Transaction time:** when did this system learn or record the assertion?
- **Valid time:** when was the assertion true in the represented world?

Memory revisions may carry `valid_from` and `valid_to`, including the precision of each bound.
Historical queries can combine `valid_at` with branch-local `known_at`, so a correction learned in
July does not appear in a reconstruction of what was known in May.

```powershell
joiny-mnemonic search "retention policy" --current
joiny-mnemonic search "retention policy" `
  --valid-at 2026-05-15 `
  --known-at 2026-05-20T00:00:00+00:00
joiny-mnemonic search "retention policy" --history
```

The stored predecessor is not rewritten when a successor closes its effective interval. Closure
is computed by the temporal projection, so the revision history remains intact.

### Rollback creates history; it does not erase it

Branches form a lineage DAG with an explicit fork cursor. A child sees its own events and the
ancestor history only up to the fork. Later parent events do not leak into the child.

```text
main:   A -- B -- C -- D
               \
trial:          E -- F

trial sees A, B, C, E, F
trial does not silently inherit D
```

This matters after restoring an old snapshot. Continuing from that point is a new historical
lineage, not an in-place rewrite of the abandoned future.

Joiny currently implements branch creation, fork-cursor visibility, task-to-branch binding, and
rollback/divergence findings. It deliberately does not advertise a universal "merge memories"
operation: choosing which later history to adopt is a domain policy, and collapsing conflicting
histories into one mutable row would destroy the evidence needed to make that choice.

### Reduced output keeps an escape hatch

Large tool output can be represented by a smaller, command-aware view. The raw output remains in
the hash-chained log, and the view retains its exact source ID and hash. Source reads and diffs pass
through unchanged; failure-bearing test and build output preserves critical evidence; a reduction
is rejected if it would be larger than the original.

This makes prompt compression reversible at the evidence boundary, even though the model's own
reasoning is not reversible.

## Protected does not mean eternal authority

Joiny has versioned active blocks for instructions, goals, constraints, decisions, and open tasks.
"Protected" means they cannot be silently lost to compaction or overwritten without history. It
does **not** mean that an old string becomes an eternal law for the agent.

Every state change has provenance. Promotion, supersession, completion, contest, and reversion are
separate transitions. Authority and origin evidence are stored separately. Retrieved memory is
data, not a command channel, and packet content does not authorize an action by itself.

In particular:

- an open task records that the task exists; it is not hidden permission to execute it;
- an old constraint remains auditable after replacement, but is not presented as the current one;
- tool output, retrieved text, and assistant-authored markers cannot promote protected state;
- autonomous closure is evidence-bound, policy-gated, consume-once, and reversible;
- manual settlement records who requested the transition and why.

The trust model is documented in [docs/security.md](docs/security.md). It is intentionally explicit
about its limit: a local process with the user's shell permissions can imitate local interfaces.
The witness registry detects ordinary rollback or divergence while it remains independent, but it
is not cryptographic non-repudiation or an OS security boundary.

## Use it without replacing your existing memory

Joiny supports three adoption shapes.

### Standalone memory core

Use the event log, typed memories, active blocks, retrieval, snapshots, hooks, and prompt assembly
as one local system.

### Provenance sidecar

Keep your existing vector store, artifact system, or state manager. Write canonical events and
revision links to Joiny, then store Joiny IDs in your own projections. Use `source` and `context`
when an answer needs exact evidence.

### Temporal and audit backend

Use Joiny only for revision history, valid-time queries, branch lineage, settlement receipts, and
integrity findings. Existing agent memory can remain the primary user-facing retrieval layer.

The Python API, local HTTP service, CLI, and MCP stdio server share the same `MemoryService`. The
SQLite database remains project-local and has no mandatory model or network dependency.

## Data model

```text
host events / explicit writes
             |
             v
  append-only canonical log  <---- exact artifacts and raw tool output
             |
             +----> active block versions
             +----> typed memory revisions + valid time
             +----> snapshots and branch-local replay
             +----> compact tool-output views
             +----> lexical / optional semantic indexes
             +----> extraction and settlement ledgers
             |
             v
     bounded prompt packets and query tools
```

The important distinction is not "SQLite versus a vector database." It is canonical history
versus rebuildable or revisable projections. Optional semantic search and reranking can improve
discovery without becoming a second source of truth.

## Quick start

Python 3.11 or newer is required. The core has no mandatory runtime dependencies.

```powershell
git clone https://github.com/Barmagloth/Joiny-Mnemonic.git
cd Joiny-Mnemonic
.\install.ps1
```

For a manual editable installation:

```powershell
python -m pip install -e .
joiny-mnemonic init
joiny-mnemonic capabilities
joiny-mnemonic verify
```

Append an event, derive a sourced fact, and inspect its history:

```powershell
joiny-mnemonic append --kind message --role user `
  --content "Decision: retain project data on uninstall by default"

joiny-mnemonic consolidate
joiny-mnemonic search "uninstall retention" --type decision
joiny-mnemonic timeline --limit 20
```

Create an explicit branch at a canonical event cursor:

```powershell
joiny-mnemonic branch-create experiment --parent main --fork-seq 120
joiny-mnemonic resume --branch experiment --text-only
```

Project-local installers are available for Claude Code, Codex, OpenCode, and OpenHands:

```powershell
joiny-mnemonic --project-root . install-hooks claude-code
joiny-mnemonic --project-root . install-hooks codex
```

Host APIs change independently of Joiny. Generated integrations are covered by repository tests,
but production use should verify the exact installed host versions. See
[docs/installation.md](docs/installation.md) and [docs/integrations.md](docs/integrations.md).

## Retrieval is a component, not the claim

The dependency-free core provides SQLite FTS5/BM25 retrieval. Local semantic search, a reranker,
a provenance-aware knowledge graph, and a local extraction backend are optional plugins.

Joiny's central claim is not that one ranking formula solves memory. Its claim is narrower:
whatever representation retrieval selects must remain connected to inspectable history, branch
lineage, temporal semantics, and exact evidence.

The project evaluates these layers separately. It publishes outcome benchmarks, methodology,
latency distributions, ablations, negative experiments, and signed reports under
[`benchmarks/results`](benchmarks/results). A feature that improves one benchmark is not silently
promoted to an always-on path; hot-path additions require timing coverage, and experimental
packing modes are closed when their mechanism does not reproduce.

See [docs/evaluation.md](docs/evaluation.md) and [docs/performance.md](docs/performance.md).

## Current boundaries

- Joiny preserves evidence; it cannot guarantee that a model reasons correctly from that evidence.
- Provenance shows where a claim came from; it does not make the claim true.
- Local witness checks detect common rollback and divergence; they do not defeat an attacker with
  the same OS permissions.
- Deterministic extraction cannot infer arbitrary unstated facts. Optional local extraction remains
  policy-controlled and provenance-bound.
- Branch lineage is implemented; a generic cross-domain branch-adoption policy is not.
- Valid-time semantics are implemented, but every application still needs a policy for vague or
  missing temporal bounds.
- Semantic retrieval and reranking are optional and add latency and model dependencies.
- Exact host integration behavior must be checked against the versions installed by the user.

## Read the model, not only the feature list

- [Architecture and invariants](docs/architecture.md)
- [Security and authority boundaries](docs/security.md)
- [Requirements traceability](docs/requirements-traceability.md)
- [Evaluation methodology](docs/evaluation.md)
- [Performance reports](docs/performance.md)
- [Backup and recovery](docs/backup.md)

Joiny-Mnemonic is not an attempt to make every old sentence immortal. It is an attempt to make
memory revisions explicit: what was observed, what was derived, what changed, which history an
agent is continuing, and where the unchanged evidence can still be found.
