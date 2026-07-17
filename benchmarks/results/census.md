# Offline failure census of saved runs — v2 (2026-07-17, no LLM)

v1 of this document overclaimed ("90% reader/synthesis") and its
retrieval/packing split was not reproducible; both defects were called
out in review and are fixed here. Script: `benchmarks/census.py`
(shallow bucketing + `--deep` reproducible evidence pass); artifacts:
`census-latest.json` (session-level buckets),
`census-deep-latest.json` (per-failure candidate pool, packed
gold-fragment texts, config and code commit).

## What `gold_coverage` actually means

In the saved rows it is SESSION-level: a gold session counts as covered
when at least one of its fragments is packed
(`longmemeval.py::build_context`). It never proved that the supporting
passage was packed. The deep pass adds that middle step with a
deterministic proxy: substring containment of the gold answer (plus
parenthesized variants, casefolded) in the packed gold-session
fragments; answers longer than 30 chars are marked indeterminate
instead of guessed.

## Raw-500: all 60 failures, three-level split (deep, reproducible)

| stage | n | meaning |
|---|---:|---|
| retrieval | 1 | no gold session in the 64-candidate pool |
| packing (session) | 5 | gold in pool, session not represented in packet |
| **passage:no** | **17** | sessions represented, but the literal answer text is NOT in any packed gold fragment — passage-selection/packing failures *inside* covered sessions |
| **passage:yes** | **11** | answer text demonstrably in the packet — proven reader/synthesis failures |
| passage:indeterminate | 26 | non-extractive answers (aggregations, preference rubrics) — proxy cannot judge |

By type: passage:no is dominated by multi-session (10) and temporal (5);
passage:yes spreads thin; indeterminate is mostly preference (12) and
temporal (8).

## Corrected conclusions (superseding v1)

1. **"90% reader" is dead.** Proven reader failures: 11/60 (18%).
   Proven retrieval-or-packing at session or passage level: 23/60
   (38%). Undetermined: 26/60 (43%).
2. **Selector/packing headroom is up to ~23 questions (~4.6pp), not
   ≤5.** Passage-level selection inside covered sessions (especially
   multi-session) is back on the table as a measured opportunity — any
   candidate selector still competes against the tuned
   session-diversity packing on a frozen set.
3. **Graph gate stays closed, for the correct reason**: the
   connectivity failure class remains unmeasured (1 retrieval miss in
   500 is not it); nothing here justifies graph indexing.
4. **No inflation at the session level**: zero correct answers without
   any gold session packed. (A weaker statement than "no leakage" — an
   alternative source could coexist with an irrelevant gold fragment;
   not measured.)

## Proxy caveats (both directions)

`passage:no` can overcount packing failures when the packet carries a
paraphrase of the answer ("weekly" vs "every week") — some of the 17
may be reader failures; `passage:yes` can overcount reader failures if
the matched string appears in a misleading context. The 26 indeterminate
rows need LLM-assisted labelling if a finer split is ever required.
The deep pass re-runs on current code (commit recorded in the artifact),
not the run-day binary.
