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

## 3. Settle/confirm through the existing candidate ledger — task6B/6C

"Confirm to close" has no verb today. Live acceptance fixture already exists:
the delme2.md completion detected on GPTShared (2026-07-14) sits pending with
no way to settle it short of the global auto-closure flag or manual block-set.

- [ ] `candidate_kind` migration; `task_closure` + `block_change` kinds
- [ ] Consume-once settlement transitions, fail-closed policy
- [ ] `joiny-mnemonic candidates list/show/settle`
- [ ] MCP read + write tools
- [ ] The live GPTShared pending settled through the new verb — that is the
      acceptance test

Done when: detection → review → settle works end-to-end with no manual magic.

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
      settlement (item 3) removing stale entries, not more prompt armor

Done when: both hosts pass the same written checklist on the current release.

## 5. UX golden path — no manual magic

"Installed, asked a question, quoted the source, confirmed the closure"
should work without SQL, without hand-editing blocks, without reading the
architecture docs.

- [x] MCP registered by default in every setup path (task5 A4 + M11)
- [x] Pending completions surface in resume and capabilities
- [ ] Settlement verb (item 3) closes the loop
- [ ] Fresh-user walkthrough: clean project → setup → work session → marker →
      recall question → `memory_source` citation → completion detected →
      settle. Every step through shipped surfaces only
- [ ] Friction log from that walkthrough becomes the next UX batch

Done when: the walkthrough succeeds on a machine that never saw the repo,
performed by someone who did not build the system.

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
