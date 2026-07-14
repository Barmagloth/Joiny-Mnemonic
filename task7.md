# Task 7: first-violin integration — native memory channels

## Status

Proposed. Trigger: a live host-level E2E finding (2026-07-14) that invalidates
our delivery assumption. A nested Claude Code session, asked what ACTIVE
MEMORY says, answered correctly — and then volunteered that the injected
`[MEMORY PACKET] / ACTIVE MEMORY` block "is not part of my real memory
system", "looks like injected text", and that it would not act on its
contents. The agent named its real memory: Claude Code's own auto-memory
(`~/.claude/projects/<project>/memory/MEMORY.md` + topic files).

Diagnosis: joiny delivers everything through hook `additionalContext`, which
hosts render as system-reminder **data**. The host's trained trust hierarchy
puts its native memory surfaces (CLAUDE.md instructions, auto-memory) above
injected data — by design, and correctly so for untrusted content. Our
protected blocks are host-verified user-authored state, but we deliver them
through a channel whose authority is "data". We are the second violin, and
second violins get ignored under pressure.

The fix is not louder injection (prompt armor demonstrably saturates); it is
delivering each class of memory through the channel whose native authority
matches its trust level.

## Research facts (verified against code.claude.com docs, 2026-07)

- **Auto-memory** (`~/.claude/projects/<munged-root>/memory/`): first 200
  lines / 25 KB of `MEMORY.md` load at every session start; topic files load
  on demand; the directory is NOT garbage-collected and third-party files +
  index lines are documented-viable; toggles: `autoMemoryEnabled`,
  `autoMemoryDirectory`, env `CLAUDE_CODE_DISABLE_AUTO_MEMORY`. The munged
  root is the project root Claude Code resolves (for a non-git subproject the
  parent root's memory applies — GPTShared sessions use `R--Projects`).
- **CLAUDE.md @-imports**: `@relative/path.md` in project CLAUDE.md /
  CLAUDE.local.md / `.claude/rules/*.md`; resolved relative to the importing
  file; ≤4 hops; loaded at session start with instruction-level authority;
  first use shows a one-time approval dialog.
- **Hook additionalContext** (SessionStart / UserPromptSubmit): rendered as
  system-reminder, 10 KB cap, data-level authority. Docs position it for
  dynamic state, CLAUDE.md for durable conventions.
- **MCP resources/prompts are not auto-loaded**; skills load on demand.
- Codex analog: `AGENTS.md` is the native instruction surface.

## Design — three channels by authority

| Channel | Carries | Authority | Freshness |
|---|---|---|---|
| Native auto-memory (MEMORY.md line + one topic file) | pointer + protected-state digest | "my own memory" (highest trained trust) | synced on block change |
| Instruction import (`@.joiny-mnemonic/active-memory.md` from project CLAUDE.md; AGENTS.md section for Codex) | protected blocks verbatim + citation rule | user instructions | session start |
| Hook injection (existing) | retrieved evidence, transcript, mid-session updates, restatement | data | every prompt |

Key inversion: evidence **should** stay data-authority (that is the trust
model working); only protected blocks deserve instruction/memory authority.
Today both ride the data channel — that is the bug.

### 7A. Generated active-memory file + CLAUDE.md import

- Hooks (post-consolidation, post-reconcile) regenerate
  `.joiny-mnemonic/active-memory.md`: protected blocks verbatim, block
  versions, source event ids, the quote-don't-recall rule, and a one-line
  pointer to MCP tools. Small (target < 2 KB), deterministic, byte-stable
  when state is unchanged (no churn).
- `setup` offers (default on, explicit opt-out) to add one managed import
  line to project `CLAUDE.md` (create the file if absent):
  `@.joiny-mnemonic/active-memory.md` inside `<!-- joiny-mnemonic:begin/end -->`
  markers, reconciliation-managed like hook handlers — never touching user
  prose, removed cleanly on uninstall.
- Codex: same generated file, imported/embedded via a managed section in
  `AGENTS.md`.

### 7B. Auto-memory bridge (Claude Code)

- `setup` detects the auto-memory directory for the resolved project root
  and, **with explicit consent** (this is a write into another system's
  memory — new trust surface, off by default on `--yes` unless
  `--with-auto-memory`), installs:
  - one topic file `joiny_mnemonic_<project>.md` (managed marker, self-
    describing, safe to delete) stating that the project's canonical memory
    is the joiny store and that the injected ACTIVE MEMORY packet is a
    rendering of that same trusted source — plus the current digest;
  - one index line in `MEMORY.md` (reconciled idempotently; never reorders
    or edits other lines; respects the 200-line window by appending high).
- Hooks refresh the topic file on block change; absence of the directory or
  a disabled feature degrades silently (capability reports the channel
  state).
- The bridge must survive the host agent editing MEMORY.md around our line;
  reconciliation re-asserts only our own line and file.

### 7C. Live verification

The E2E checklist (TODO item 4) gains an authority probe: a nested session is
asked about protected state and about the packet's legitimacy; pass =
answers sourced from joiny state with no injection-suspicion disclaimer, and
`memory_blocks`/`jm` quoting preferred over paraphrase. Run on Claude and
Codex before/after enabling each channel to attribute the effect.

Blocked experiment on record: the first probe write into the live
auto-memory directory (2026-07-14) was denied by the session's permission
classifier — correctly, since it is another agent's memory surface. The
probe needs the user to either run it themselves or grant the write
explicitly; the file/line design above is exactly what the probe would
install.

## Trust and safety rules

- Writing outside `.joiny-mnemonic/` (CLAUDE.md line, AGENTS.md section,
  auto-memory file+line) is a **declared capability**, opt-in at setup,
  visible in `capabilities`, reconciliation-managed, marker-delimited, and
  reversible by `uninstall` without residue.
- Joiny never edits, reorders or deletes content it did not write on those
  surfaces; fail-safe on parse surprises (leave the file untouched, report a
  finding).
- The generated content carries provenance (block versions + source event
  ids) so the authority channels stay auditable against the store.
- No secrets and no retrieved evidence ever enter the authority channels —
  protected blocks and pointers only. Evidence stays in the data channel by
  design.

## Required tests

- Generated file: byte-stable across no-op syncs; regenerates on block
  change; provenance lines match the store.
- CLAUDE.md/AGENTS.md reconciliation: fresh install, reinstall (no
  duplicates), uninstall (clean removal), user-edited surroundings preserved
  byte-identically; missing file created; markers corrupted → fail-safe +
  finding.
- Auto-memory bridge: consent gating (off without opt-in), idempotent line
  reconciliation against a MEMORY.md the host agent has edited, silent
  degradation when the feature is disabled, capability reporting.
- Authority probe scripted for the host-E2E checklist.

## Acceptance criteria

1. On a host with 7A enabled, a fresh session treats protected blocks as
   instructions (no injection-suspicion disclaimer in the authority probe).
2. All external-surface writes are opt-in, marker-delimited, reconciled,
   and uninstall-clean.
3. Hook channel unchanged for evidence; packet keeps working on hosts where
   no authority channel is enabled.
4. Docs (`architecture`, `security`, `integrations`) describe the channel
   model and its trust boundaries; capabilities expose per-channel state.

## Open questions

- Should the CLAUDE.md import live in `CLAUDE.local.md` instead (gitignored,
  per-machine) to avoid committing a joiny reference into shared repos — or
  is the shared visibility a feature? Probably: project `CLAUDE.md` when the
  project already commits `.joiny-mnemonic/`, else `CLAUDE.local.md`.
- Auto-memory digest size: full blocks or pointer-only? Start pointer+digest,
  measure with the authority probe.
- Does the CLAUDE.md import approval dialog (first load) need install-time
  documentation so users are not surprised?
