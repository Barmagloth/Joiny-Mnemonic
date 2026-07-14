# Task 6: hot-path discipline and unified candidate settlement

## Status

Proposed next milestone, restructured after review. The original draft harvested
Shepherd's (`shepherd-agents/shepherd`) settlement contract as a new
proposals/proposal_effects/proposal_transitions subsystem; review found that the
repo already ships exactly that machine: `extraction_candidates` +
`candidate_transitions` + `candidate_memory_links` + `candidate_current_status`
are an append-only, consume-once status ledger with actor/rule/source-event
provenance. Building a parallel twin would violate this task's own
anti-complexity goal. Task 6 therefore **generalizes the existing candidate
machinery** instead of adding a second one, and narrows scope to the
memory/state domain.

The architectural gap this closes is real and already visible in production
paths: `task_completion_detected` exists, the pending line in resume and
capabilities exists, but there is no formal confirm/accept/reject verb — the
only ways to settle a detected completion are enabling the global auto-closure
flag or a manual `block-set`. `block_change_requested` is the same category: a
request event with no formal settlement. Both become candidates under one
settlement model.

Complexity motivation stands regardless of measurement point: `storage.py` was
~4262 lines when the first draft was written and ~4476 a day later after the
task5 backlog pass — the file grows by hundreds of lines per working day, which
is the monster argument demonstrating itself.

Execution order is strict: 6A (measure and isolate, zero behavior change) →
6B (settlement semantics) → 6C (surfaces).

## Non-goals

- No new proposal tables — the candidate ledger is the single settlement
  machine.
- No file-effect or command-result runtime semantics. A generic
  `candidate_kind` enum slot is reserved, but nothing executes file changes.
- No Shepherd dependency, no Shepherd bridge, no sandbox runtime — deferred
  entirely (see Deferred below), like `hindsight-bridge` before it.
- No LLM judge or meta-agent in the core.
- No background agent execution from hooks.
- No automatic application of any candidate; settlement is explicit.
- No rewriting canonical events, memory records, task records or block history.
- No large new logic inside `storage.py`, `hooks.py` or `MemoryService` beyond
  thin delegating surfaces; settlement logic lives in focused modules.

## Task 6A — hot-path measurement and storage split

Zero behavior change. Ships first because 6B must be provably cold.

- Hook-path timing benchmark measuring, at minimum:
  - capture-only PostToolUse;
  - PostToolUse with reducer;
  - UserPromptSubmit with resume injection;
  - PreCompact/PostCompact compaction path;
  - reconciler path with and without pending candidates.
- Budgets asserted as benchmark gates (extend the existing gate set — the
  p95-style gates in `joiny-mnemonic-benchmark` are the pattern). Agreed
  budgets are recorded in the report, and regressions fail `--assert-gates`.
- Cold-feature invariant, documented and tested: optional bridges, external
  runners and report tooling must not import or execute during normal hook
  delivery, resume or search.
- Storage split/isolation: move cohesive sections (candidate/finding/extraction
  storage is ~4k lines of the file) toward focused modules or clearly bounded
  sections with their own tests. No behavior change; suite stays green
  throughout.
- The timing report is provenance-stamped and checked into
  `benchmarks/results/` (report stamping itself shipped with task 5:
  `report_signing.py`).

## Task 6B — general candidate settlement

Extend the existing ledger, not duplicate it.

Data model changes (one schema migration):

- `extraction_candidates` gains `candidate_kind` (default `extraction` for all
  legacy rows). New kinds in this task: `task_closure`, `block_change`.
  The enum is open for future kinds; only these three get semantics now.
- Candidate provenance rules are unchanged: every candidate cites its source
  events; every transition cites an actor, a rule id and a source event.

Semantics:

- **task_closure**: the reconciler's detection (unchanged canonical
  `task_completion_detected` event) additionally creates a `task_closure`
  candidate citing the detection and evidence events. Settlement verbs align
  with the existing transition vocabulary; add new statuses only if the
  existing set cannot express accepted/rejected/applied/discarded.
  An accepted closure writes through the existing block/task APIs (new
  `open_tasks` version, superseded task memory) and cites the candidate and its
  evidence — exactly what the auto-closure flag does today, but per-candidate
  and explicit.
- **block_change**: the `?`-marker guard and future request paths create a
  `block_change` candidate instead of (or in addition to, during migration) the
  loose `block_change_requested` state event. Old events stay readable;
  acceptance writes through `set_active_block` citing the candidate.
- Settlement is consume-once: repeated identical settlements are idempotent,
  conflicting settlements fail closed — the existing candidate-transition
  discipline already guarantees this shape.
- `settlement_policy` (per kind): which actors may settle. Defaults fail
  closed: `task_closure` and `block_change` settle only from trusted origins —
  local operator (CLI), a host-verified user event, or a policy-ledger flag
  that explicitly delegates to the agent (MCP write). Untrusted public-API
  text can never settle anything (same H1 discipline as completion evidence).
- `enforcement_level` recorded on settlement: `recorded_only` or `advisory`.
  Nothing in this task may claim OS enforcement; contracts are audit evidence,
  not magic authority.

## Task 6C — settlement surfaces

- CLI: `joiny-mnemonic candidates list --kind --status`, `candidates show <id>`,
  `candidates settle <id> --transition --reason`.
- MCP: one read tool (`memory_candidates`) and one explicit write tool
  (`memory_settle_candidate`) requiring candidate id + transition; tool
  descriptions state that settlement is auditable and gated by policy, and
  never imply OS isolation.
- Resume/prompt: active candidates appear only as a bounded index line
  (generalizing the existing `[PENDING TASK COMPLETIONS ...]` line); full
  candidate content is never injected by default — agents quote it through
  tools (A4 citation-over-recall discipline).

## Required tests

- 6A: timing benchmark produces a stamped report; gates fail on budget
  regression; hook delivery imports no optional/cold modules (import-graph or
  sys.modules assertion); behavior-freeze — full suite green with no
  functional diffs.
- 6B: legacy extraction rows migrate with `candidate_kind='extraction'` and
  identical behavior; task_closure candidate created alongside detection;
  settlement idempotent on repeat, fail-closed on conflict; accepted closure
  writes through existing APIs and cites the candidate; rejected/discarded
  candidates never touch active state but remain searchable evidence;
  untrusted public-API text cannot settle; block_change candidate path covers
  the `?`-marker guard end-to-end.
- 6C: CLI round-trip list/show/settle; MCP read and write tools through the
  real server handshake; resume line is bounded and disappears once settled.

## Acceptance criteria

1. Settlement semantics are append-only, provenance-bound, branch/task aware,
   and expressed entirely through the existing candidate machinery.
2. No candidate path mutates active blocks or typed memories except through an
   explicit accepted settlement that cites the candidate.
3. Hook delivery, resume and search remain cold with respect to settlement
   tooling; the 6A timing report proves it with numbers.
4. Full suite passes at each phase boundary (6A, 6B, 6C land separately).
5. The stamped timing report is checked into `benchmarks/results/`.
6. `docs/architecture.md`, `docs/security.md` and
   `docs/requirements-traceability.md` state the unified candidate model and
   its trust limits.
7. `storage.py`/`hooks.py`/`service.py` grow only by facade/migration code;
   settlement logic lives in focused modules.

## Deferred (Task 7 material, not in scope)

- Shepherd bridge in any form: early alpha, no Windows support — on a
  Windows-first project a bridge to a runtime that cannot run here is pure
  scope inflation. Revisit when Shepherd stabilizes or a WSL/Linux host
  matters.
- Authority-contract remainder: `bindings` with git identity,
  `execution_backend`, OS-enforced levels — YAGNI until a real executor
  exists.
- `file_change` / `command_result` candidate semantics (enum reserved, runtime
  deliberately unbuilt).
- Import of external retained-artifact runs as candidates.

## Open questions

- Should settlement tie into `TaskManager` status transitions, or stay
  independent and only cite `task_key`?
- Are the existing candidate transition statuses sufficient for settlement
  verbs, or is a minimal additive extension (e.g. `applied`) required? Decide
  against the real vocabulary during 6B design, not by adding statuses
  speculatively.
- Should unsettled candidates be visible through ordinary `search`, or only
  through candidate tools until accepted?
