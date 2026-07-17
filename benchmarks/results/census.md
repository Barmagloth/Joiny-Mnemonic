# Offline failure census of saved runs (2026-07-17, no LLM)

Deterministic bucketing of every persisted question row (user taxonomy);
script: `benchmarks/census.py`, raw buckets in `census-latest.json`. The
saved rows record *packed* sessions, so the retrieval-vs-packing split
for sub-coverage cases was resolved by a local retrieval re-run (pool at
limit 64, no LLM) for exactly those questions.

## Raw-500 (the published 88.0% run): all 60 failures located

| class | n | share of failures |
|---|---:|---:|
| **reader/synthesis** — gold fully packed, answer wrong | **54** | **90%** |
| packing — gold in the 64-pool, pushed below the budget line | 5 | 8.3% |
| retrieval — gold absent from the pool (gpt4_4929293b) | 1 | 1.7% |
| leakage — correct with zero gold packed | 0 | 0% |

Reader failures by type: multi-session 18, temporal-reasoning 16,
preference 12, single-session-assistant 4, knowledge-update 3,
single-session-user 1.

Probe arms show the same shape: keyed-KU 5/5 failures are full-coverage,
keyed-preference 9/9, abstention baseline 3/3.

## Consequences for the four-point scope

1. **The main failure class is the last mile**: sources are in the
   packet; the answer still goes wrong. Everything measured this week
   (stale-status collapse, enumeration, abstention, preference breadth)
   lives in this bucket.
2. **Graph gate: firmly closed by data.** One candidate
   connectivity-style miss in 500 questions — and it still needs a check
   whether it is connectivity or plain lexical mismatch. No failure
   class exists for graph indexing to fix.
3. **Set-selector headroom is now a number: ≤5 questions (~1pp).** Any
   selector work must justify itself against that ceiling on this
   benchmark; effectively deprioritized until a workload with different
   packing pressure exists.
4. **Leakage clean**: no correct answers without gold in the packet —
   the 88.0% number is not inflated by alternative-source luck.

Caveat: the census inherits the judge's verdicts (a "reader failure" is
a judged-wrong answer; judge-sensitive types like preference blur the
boundary), and the packing/retrieval split was recomputed on today's
code, not the run-day binary — chain-of-custody for that split is
code-version-loose, acceptable for prioritization.
