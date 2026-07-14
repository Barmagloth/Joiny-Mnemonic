# TODO — validation and usability

The core works; the project is unvalidated and the UX has sharp edges. This
file tracks the short list of work that actually changes that assessment —
benchmarks people can check, settlement without manual magic, repeatable
host-level proof. Ordered by impact. Statuses updated as items land; detailed
specs live in the task files, not here.

## 1. Real LongMemEval-S run — IN PROGRESS

Baseline v1 running now (500 questions, local `claude -p` runner, Sonnet).
Interim signal: single-session-user strong, multi-session and preference weak
— context budget 4096 tokens / retrieval limit 24 look undersized for
multi-session aggregation, and the answer-only-from-packet prompt pushes
preference questions into abstention.

- [x] Harness (task5 Part C), local runner bridge, resumable runs
- [x] Signed reports (provenance + artifact hashes)
- [x] Baseline v1 checked in (2026-07-14, signed): 57.6% overall —
      single-session-user 84.3%, assistant 82.1%, knowledge-update 79.5%,
      temporal-reasoning 59.4%, multi-session 26.3%, preference 23.3%,
      abstention 28/30
- [x] Error analysis done (2026-07-14 probe series, 20 multi-session
      questions): pool holds 100% of gold sessions at limit 128 — retrieval
      exonerated; rank/12k 6/20, cap3 5/20, breadth 0/11 (killed), rank/20k
      5/20 — multi-session plateaus at ~25-30% regardless of packing and
      budget; the wall is turn-sized fragments (400-1200 tok) vs one-shot
      aggregation
- [ ] Tuned run v2 in flight: budget 12288 / limit 64 / rank packing +
      validated fixes (query_timestamp anchor, preference synthesis,
      enumerate prompt); sweep-projected ~63-68%
- [ ] Future multi-session lever (separate work, not config): finer
      ingestion granularity (sub-turn events) and/or two-pass aggregation
      in the answering flow
- [ ] README section with our numbers — ours only, no other systems' scores

Done when: a signed report lives in `benchmarks/results/` and the README
states the figure and the configuration that produced it.

## 2. Hook-path timing report — task6A

We shipped hot-path fixes (M5/M6) without measurements; that is debt.

- [ ] Timing benchmark: capture-only PostToolUse; PostToolUse+reducer;
      UserPromptSubmit with resume injection; PreCompact/PostCompact;
      reconciler with/without pendings
- [ ] Budgets asserted as gates in `joiny-mnemonic-benchmark`
- [ ] Cold-feature invariant test (hook delivery imports no optional modules)
- [ ] Stamped report checked into `benchmarks/results/`

Done when: a regression in hook latency fails `--assert-gates`.

## 3. Autonomous state maintenance with auditable undo — task6B/6C

The user must not police memory. Detection is already automatic; the default
must be automatic *closure* on strong deterministic evidence, with a passive
one-line notice in the next injection and a one-command revert (block history
makes undo lossless — that is what licenses the automation). Manual
settlement verbs exist for the ambiguous tail and for reversal only; a
growing pending queue is a detection-quality bug, not a UX feature. Live
fixture: the delme2.md completion detected on GPTShared (2026-07-14) should
have closed itself.

- [ ] `candidate_kind` migration; `task_closure` + `block_change` kinds
- [ ] Evidence-strength ladder; strong evidence auto-applies by default
      (actor `system`, full audit trail)
- [ ] Bidirectional reconciliation: re-added marker reopens a closed entry
      (auto-undo, `contested`); invalidated evidence reverts the closure —
      wrong closures are caught by the system, not by user vigilance
- [ ] Human-visible notice at action time through the host's user-facing
      hook output (with the ready undo command) + auto-action delta in the
      session-start digest
- [ ] Consume-once settlement transitions, fail-closed policy, first-class
      `undo`; a reverted closure never re-applies from the same evidence
- [ ] `joiny-mnemonic candidates list/show/settle/undo` + MCP read/write
- [ ] Acceptance: a fresh GPTShared-style scenario closes the task with zero
      user actions; re-adding the marker reopens it with zero user actions;
      `undo` restores the entry losslessly

Done when: the common case needs no user action at all, and the wrong-closure
case costs one command.

## 4. Host-level E2E: Claude + Codex, repeatable

One-off passes rot. This should be a checklist (or script) run per release.

- [x] Claude Code pass (2026-07-14): store opens on new code, FTS signal
      rebuild transparent, hooks fire, injection delivered, reconciler
      detects the historical completion, temporal fusion live in search,
      reduction-report clean
- [ ] Codex pass: same checklist (hooks, injection, recall, PostCompact
      silence)
- [ ] Checklist written down (docs/host-e2e.md) so any release can be
      re-verified in ~15 minutes
- [ ] Live finding tracked: verb-flip reproduced 2026-07-14 ("создать" in the
      block, "удалить" in a nested session's paraphrase) — the durable fix is
      autonomous closure (item 3) removing stale entries by itself, not more
      prompt armor and not user vigilance

Done when: both hosts pass the same written checklist on the current release.

## 5. UX golden path — no manual magic

"Installed, asked a question, quoted the source, confirmed the closure"
should work without SQL, without hand-editing blocks, without reading the
architecture docs.

- [x] MCP registered by default in every setup path (task5 A4 + M11)
- [x] Pending completions surface in resume and capabilities
- [ ] Autonomous closure (item 3) removes the last manual step
- [ ] Fresh-user walkthrough: clean project → setup → work session → marker →
      recall question → `memory_source` citation → task completes → closure
      happens by itself, notice visible, nothing to confirm. Every step
      through shipped surfaces only
- [ ] Friction log from that walkthrough becomes the next UX batch

Done when: the walkthrough succeeds on a machine that never saw the repo,
performed by someone who did not build the system — and the memory required
zero maintenance actions from them.

## 6. Recall / extraction quality

LongMemEval (item 1) measures end-to-end recall. Extraction needs its own
small eval: markers and automatic extraction against a fixture corpus with
known expected candidates — precision/recall per marker type, quarantine
behavior on the fuzzy tail. No LLM in the loop; the corpus is hand-labeled
once.

- [ ] Fixture corpus + expected-candidate labels
- [ ] Extraction eval runner + signed report
- [ ] Recall/precision figures in the README next to the LongMemEval number

Done when: extraction changes are gated by measured precision/recall, not
vibes.
