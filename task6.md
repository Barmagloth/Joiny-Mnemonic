# Task 6: proposal settlement without growing a slow monster

## Status

Proposed next milestone. The trigger is a design harvest from Shepherd
(`shepherd-agents/shepherd`) plus an internal complexity check after Task 5.

Shepherd's useful idea is not "another memory backend". It is a runtime
contract for agent effects: an agent run produces retained outputs and
changesets; those effects are inspected and then settled exactly once with a
decision such as select/apply/release/discard. Permissions are declared on the
task signature, per bound repository, and lower to the OS sandbox on supported
hosts.

Joiny-Mnemonic should adopt the settlement model, not Shepherd as a required
dependency. Shepherd is early alpha, Windows is unsupported, and its syscall
enforcement is macOS/Linux only. Joiny remains the provenance, memory and audit
core; Shepherd-like systems can later become optional execution backends.

This milestone must also pay down complexity. Current scale is already high:

- `src/joiny_mnemonic/storage.py`: about 4262 lines.
- `src/joiny_mnemonic/hooks.py`: about 1100 lines.
- `src/joiny_mnemonic/service.py`: about 904 lines.
- `src/joiny_mnemonic/cli.py`: about 840 lines.
- core package: about 16812 Python lines.
- tests: about 6776 Python lines.

The project is not yet a slow monster, but the next feature can make it one if
it adds another always-on subsystem. Task 6 therefore has two equal goals:

1. capture proposed effects and explicit settlement decisions with exact
   provenance;
2. keep hook delivery, resume and search predictable by making proposal work
   cold-path and budgeted.

## Non-goals

- No mandatory Shepherd dependency.
- No mandatory sandbox runtime.
- No background agent execution from hooks.
- No automatic application of file, command or memory proposals.
- No LLM judge or meta-agent in the core.
- No rewriting canonical events, memory records, task records or block history.
- No large new logic inside `storage.py`, `hooks.py` or `MemoryService` unless it
  is a thin delegating surface.

## Part A - proposal ledger

Add a small append-only proposal layer for agent effects. A proposal is a
claim that some external or internal run produced candidate effects. The
proposal itself is not an applied effect.

Data model:

- `proposals`
  - id, branch_id, task_key, run_id, source_event_ids, status, created_at,
    metadata_json;
  - status is derived from transitions and cached only if the existing storage
    pattern requires it; append-only transitions remain authoritative.
- `proposal_effects`
  - proposal_id, effect_id, effect_type, binding_name, path, content_hash,
    summary, metadata_json;
  - effect types: `file_change`, `memory_change`, `command_result`,
    `artifact`, `note`;
  - every effect cites either a canonical event, an artifact hash, or an
    external retained-output hash.
- `proposal_transitions`
  - proposal_id, transition, actor, source_event_id, created_at,
    metadata_json;
  - transitions: `proposed`, `selected`, `applied`, `released`, `discarded`,
    `superseded`, `failed`.

Rules:

- A proposal is immutable once written.
- A proposal can be settled once. Repeated identical settlement requests are
  idempotent; conflicting settlement requests fail closed.
- `applied` records that the effect was accepted by a trusted settlement
  action. The actual canonical file/memory/block event still gets its own
  normal write path and cites the proposal.
- `discarded` proposals remain searchable evidence. They are not active memory
  and do not affect ranking.
- Proposal IDs and effect IDs must resolve through `memory_source` /
  `memory_context`-style exact provenance, or through a new proposal-specific
  source command if widening the existing tool would break compatibility.

## Part B - authority contract

Adopt Shepherd's "permission in the contract" idea without relying on its
runtime.

Add a reviewable task/run contract object:

- `bindings`: named roots, each with absolute resolved root, git identity,
  and declared authority: `read_only`, `read_write`, `artifact_only`.
- `declared_effects`: allowed effect types per binding.
- `settlement_policy`: who or what may select/apply/release/discard.
- `execution_backend`: `external`, `manual`, `shepherd`, `none`.
- `enforcement_level`: `recorded_only`, `advisory`, `os_enforced`.

Trust boundary:

- On Windows, Shepherd-style enforcement is `recorded_only` or `advisory`
  unless a future backend proves real isolation. Do not imply OS enforcement.
- A shell-capable agent can spoof ordinary text. Strong settlement still needs
  trusted host evidence, an explicit user event, or a future signed UI receipt.
- Contracts are provenance evidence and audit controls, not magic authority.

## Part C - settlement surfaces

Expose proposal operations through CLI, Python and MCP/HTTP only where they add
clear value.

CLI:

- `proposal create --from-artifact ...`
- `proposal list --task ... --status ...`
- `proposal show <id> --include-effects`
- `proposal select <id>`
- `proposal apply <id> --yes`
- `proposal discard <id> --reason ...`

MCP:

- Prefer one `memory_proposals` read tool plus one explicit
  `memory_settle_proposal` write tool.
- Write calls must require an explicit proposal id and transition.
- Tool descriptions must say settlement is auditable and may not imply OS
  isolation.

Prompt/resume:

- Active proposals may appear only as a short warning/index line.
- Full proposal content is not injected by default.
- Agents should quote proposal details through tools before applying or
  summarizing them.

## Part D - optional Shepherd bridge

Design a bridge, but keep it optional and cold-path.

Bridge behavior:

- detect Shepherd availability and supported host;
- import a Shepherd run as a Joiny proposal;
- pin retained-output hashes;
- map Shepherd bindings to Joiny bindings;
- map Shepherd `select/apply/release/discard` outcomes to proposal transitions;
- record Shepherd version, run id and enforcement mode.

Hard constraints:

- no Shepherd import at module import time;
- no Shepherd command on hook delivery;
- no failure in the bridge can fail canonical capture;
- unsupported Windows host reports `unsupported` capability, not a degraded
  fake sandbox.

## Part E - anti-monster work

Before or alongside proposal implementation, split hot-path responsibilities
without changing behavior:

- Move proposal code into new modules such as `proposals.py` and
  `proposal_cli.py`; keep `MemoryService` as a thin facade.
- Do not add broad generic helpers to `storage.py`. Prefer small storage
  sections with clear SQL and focused tests.
- Add a hook-path timing report that measures:
  - capture-only PostToolUse;
  - PostToolUse with reducer;
  - UserPromptSubmit with resume injection;
  - PreCompact/PostCompact compaction path;
  - reconciler path with and without pending candidates.
- Add a "cold feature" rule: optional bridges, external runners and report
  tooling must not execute during normal hook delivery, resume or search.
- Update benchmark reporting so checked-in reports always identify project
  root, commit, dirty state and backing artifacts.

## Required tests

- Proposal creation stores immutable proposal and effect rows with exact source
  or artifact hashes.
- Settlement is consume-once: same transition is idempotent, conflicting
  transitions fail.
- Applying a memory/block proposal writes through the existing explicit API and
  cites the proposal; discarded proposals never become active memory.
- Proposal source/context lookup returns the proposal event, effect metadata and
  backing artifact/source events.
- Untrusted public API text cannot mark a proposal as applied.
- Windows Shepherd bridge capability is `unsupported` or `advisory`, never
  `os_enforced`.
- Bridge import failure is isolated and does not fail canonical capture.
- Hook delivery does not import Shepherd and does not scan proposal artifacts.
- Resume contains only bounded proposal index lines.
- Performance tests assert no regression beyond agreed budgets.

## Acceptance criteria

1. Proposal and settlement semantics are append-only, provenance-bound and
   branch/task aware.
2. No proposal path mutates active blocks or typed memories except through an
   explicit accepted settlement that cites the proposal.
3. Hook delivery remains cold with respect to Shepherd/external runners.
4. Full suite passes.
5. A hook-path timing report is produced and checked into
   `benchmarks/results/` with provenance stamping.
6. `docs/architecture.md`, `docs/security.md`, `docs/integrations.md` and
   `docs/requirements-traceability.md` state the proposal model and its trust
   limits.
7. The implementation does not grow `storage.py`, `hooks.py` or `service.py`
   by more than the minimum facade/storage code needed; most new logic lives in
   focused proposal modules.

## Open questions

- Should proposal settlement be tied to `TaskManager` status transitions, or
  remain independent and only cite `task_key`?
- Should `apply` ever write files directly, or should it only emit a reviewed
  plan for an external tool to apply?
- Should proposal effects be visible through ordinary `search`, or only through
  proposal-specific tools until accepted?
- What is the first real backend to import: Shepherd on WSL/Linux, a manual
  retained-artifact importer, or our existing external task runner output?
