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
- [x] Final run (2026-07-15, signed, product plugin path — semantic-local +
      reranker-local, 12288/64/rank): **88.0% overall** (440/500).
      Per type: ssu 98.6 / ku 96.2 / ssa 92.9 / ms 85.7 / temporal 84.2 /
      preference 60.0; abstention 28/30; retrieval recall ~100%; token
      saving 92.9%. Multi-session plateau broken by full-pool cross-encoder
      reranking in the engine (26.3% -> 85.7%)
- [x] README section with our numbers — ours only, no other systems' scores
- [ ] Remaining error-analysis target: preference (60%) — cross-encoder
      optimizes question-relevance while preference answers need breadth of
      taste evidence; and the temporal tail (84.2%)
- [ ] A/B with LLM extraction (--ingest distill, facts alongside verbatim
      through the derive path) — mechanism shipped, run pending
- [x] Cross-family re-judge done (2026-07-14, GPT-5.4 over all 500
      persisted answers, byte-pinned rows and dataset, verified by recount
      from raw batches, report signed): **87.6%** vs 88.0%, agreement
      98.8%, 6 flips (4 down, 2 up), zero flips in multi-session. The
      same-stack-judge caveat is now empirically bounded at ~0.4pp
- [x] Opus re-judge done (2026-07-15, signed): 89.0%, 9 flips (7 of them
      preference). Triangulation Sonnet/GPT-5.4/Opus = 88.0/87.6/89.0 —
      ±0.7pp; multi-session identical under all three judges; preference
      is the judge-sensitive type (60.0–76.7%)
- [x] Flip-set audit done (2026-07-15, benchmarks/results/flip-audit.md):
      12 unique flips; against the 3-judge majority the published number is
      slightly conservative (majority consensus 88.2%); one genuine
      leniency instance found and documented; wrong-lead-right-string
      archetype exists, adjudicated split; preference carries 7/12 flips;
      zero multi-session flips under any judge
- [x] Prompt ablation done (2026-07-15): plain prompt 70.0% vs tuned 86.7%
      on the same stratified 60 under the identical product stack — the
      benchmark-tuned prompt contributes ~17pp; README states it and how to
      reproduce (LME_PLAIN_PROMPT=1)
- [x] Variance repeats done (2026-07-15): frozen config, same stratified
      60, three points — 86.7 / 91.7 / 90.0 (published subset is the
      lowest); 5pp band on n=60 ≈ ±1.5-2pp at n=500; preference confirmed
      as the volatile type. Methodology hardening: COMPLETE — all seven
      weaknesses measured or closed

Done when: (achieved for the headline; error-analysis items continue)

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
