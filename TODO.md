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
- [ ] Baseline v1 report checked in (run in flight)
- [ ] Error analysis: multi-session and preference misses against
      `retrieved_ids` — retrieval miss vs synthesis miss
- [ ] Tuned run v2 (budget / retrieval limit; prompt stays honest)
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
      (actor `system`, full audit trail), passive notice line
- [ ] Consume-once settlement transitions, fail-closed policy, first-class
      `undo`
- [ ] `joiny-mnemonic candidates list/show/settle/undo` + MCP read/write
- [ ] Acceptance: a fresh GPTShared-style scenario closes the task with zero
      user actions; `undo` restores the entry losslessly

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
