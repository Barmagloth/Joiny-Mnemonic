# Judge flip-set audit (2026-07-15)

Manual review of every question where at least one of the three judges
(Sonnet — original, GPT-5.4 — independent family, Opus — same family) disagreed
with the published verdict. 12 unique questions out of 500. Verdict codes:
S/G/O = Sonnet/GPT-5.4/Opus, T = correct, F = incorrect. "Majority" is the
2-of-3 vote.

| # | id | type | S | G | O | Majority | Original vs majority |
|---|----|------|---|---|---|----------|----------------------|
| 1 | 06f04340 | preference | F | T | T | T | too strict |
| 2 | 0a34ad58 | preference | F | F | T | F | matches |
| 3 | 0edc2aef | preference | F | F | T | F | matches |
| 4 | 1c0ddc50 | preference | T | T | F | T | matches |
| 5 | 32260d93 | preference | F | F | T | F | matches |
| 6 | 59524333 | knowledge-update | T | F | T | T | matches |
| 7 | 75f70248 | preference | F | F | T | F | matches |
| 8 | 778164c6 | assistant | T | F | T | T | matches |
| 9 | b6025781 | preference | F | T | T | T | too strict |
| 10 | ba61f0b9 | knowledge-update | T | F | F | F | **too lenient** |
| 11 | faba32e5 | single-session-user | T | F | T | T | matches |
| 12 | gpt4_68e94288 | temporal | F | F | T | F | matches |

## Findings

1. **Net bias of the published number is slightly conservative.** Against the
   three-judge majority vote, the original verdicts undercount twice (cases 1,
   9 — grounded preference syntheses that Sonnet rejected for their hedged
   openings) and overcount once (case 10). Majority-consensus accuracy would
   be 441/500 = **88.2%**, +0.2pp above the published 88.0%.

2. **One genuine leniency instance (case 10, ba61f0b9).** A knowledge-update
   question ("how many women on Rachel's team") where the answer surfaced the
   5-vs-6 contradiction across sessions and declined to resolve it. Sonnet
   accepted; both re-judges correctly rejected — a knowledge-update question
   asks for the *current* value, and the correct resolution (later session
   supersedes) is exactly what the type tests. Ironically, our own supersession
   machinery embodies the right rule; the *answer synthesis* failed to apply
   it, and the original judge let that through.

3. **The "wrong lead, right string" archetype exists but is adjudicated
   split, not rubber-stamped** (cases 8, 11 — Escovitch-first, and
   "packet doesn't specify... so 24 hours"). GPT-5.4 rejects these; Sonnet
   and Opus accept. Under the verbatim A.4 prompt both readings are
   defensible; the majority accepted both. This is a protocol property, not a
   stack artifact — but our verbose answering style does encounter it more
   often than a terse style would.

4. **Preference dominates instability**: 7 of 12 flips. The rubric asks the
   judge to weigh multi-part preference coverage ("uses Suica AND TripIt"),
   and partial coverage lands differently per judge. Per-type CI at n=30 is
   ±17.5pp; the 60.0% published figure should be read as "roughly 50-77%".

5. **Types that carry the headline are stable**: zero flips in multi-session
   (133 questions) across all three judges; temporal-reasoning has exactly
   one flip in 133.

## Method

Verdicts compiled from the signed per-question JSONL (original), the raw
GPT-5.4 batch verdict files (with rationales), and the signed Opus re-judge
report; all three share the byte-pinned rows and dataset. Classification
performed by reading each answer against the dataset gold/rubric.
