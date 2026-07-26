# Manual audit of the 28 census cases (2026-07-17)

Scope per review decision: hand-check the 17 `passage:no` and 11
`passage:yes` cases before any selector design; the 26 indeterminate
stay unexamined until they would change an implementation choice.
Evidence: `census-deep-latest.json` (packed gold fragments) +
`longmemeval-latest.jsonl` (persisted answers). Verdicts are mine
(assistant), per-case, disputable by construction — the fragments are
persisted for re-review.

## Proxy bug found first

The deep-pass answer-containment proxy filtered variants shorter than 2
chars, so every bare-count gold ("3", "4") had zero variants and fell
into `passage:no` automatically. Aggregate answers are computed, never
quoted — they now get their own `passage:aggregate` bucket
(census.py fixed, artifact regenerated).

## passage:yes (11) — all confirmed reader-side

| qid | subclass |
|---|---|
| 51a45a95 | question comprehension (answered date, asked where) |
| dd2973ad | temporal linking across sessions |
| 9ee3ecd6 | **conflicting versions** (100 vs 300, picked wrong) |
| 73d42213 | recall/comprehension (lean; string match context unverified) |
| 09ba9854 | arithmetic over quoted figures |
| 370a8ff4 | temporal arithmetic (81 days vs gold 15 weeks anchoring) |
| gpt4_65aabe59 | temporal comparison (ordered two setups wrong) |
| gpt4_fe651585 | temporal comparison (who first) |
| 852ce960 | **conflicting versions / stale-vs-update** (350k vs 400k; the keyed-distill arm fixed this one) |
| 01493427 | enumeration conclusion (both figures found) |
| 561fabcd | recall (picked the wrong settled name) |

## passage:no (17) — the class dissolves under audit

| verdict | n | qids |
|---|---:|---|
| actually reader (enumeration/arithmetic/temporal; count-answer proxy artifact) | ~7 | 81507db6 (overcounted), 37f165cf (both page counts packed, dates misread), gpt4_85da3956 (visit passage packed, week arithmetic), gpt4_70e84552 (both dates packed, comparison inverted), gpt4_2d58bcd6 (both packed, mis-ordered), d851d5ba (figures largely packed, sum wrong), 1b9b7252 (resources list packed, question mishandled) |
| **true passage miss** — supporting passage genuinely absent from packet | ~6 | gpt4_8279ba03 (clear: only a pellets reply packed, the smoker purchase line absent), ba358f49 (age passage absent), a11281a2 (baseline-250 passage absent), gpt4_731e37d7 (workshop price passages absent), 07741c45 (final shoe-rack state absent), bf659f65 (third album passage, lean) |
| uncertain (aggregation across many fragments, unverifiable without full packet read) | ~4 | 0a995998, c4a1ceb8, 2ce6a0f2, gpt4_93159ced |

## Bottom line

- **True passage misses: ~6 of 60** (+5 session-level packing) →
  realistic selector/packing headroom ≈ **~2pp**, narrowed again from
  census-v2's "up to 4.6pp".
- **The largest single addressable cluster is not retrieval at all:
  temporal comparison/arithmetic at answer time (~7-8 of the 28)** —
  facts packed, ordering or subtraction wrong. Second: conflicting
  versions presented without status (2 confirmed; the keyed-distill arm
  already fixed one of them).
- Selector design stays unjustified; if anything earns a probe next, it
  is answer-time temporal discipline and status/recency presentation —
  both last-mile, both already flagged by the four-point scope.
