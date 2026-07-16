# Distill A/B — LLM-extracted facts alongside verbatim turns (2026-07-16)

**Question.** Does the Phase-B ingestion shape — LLM-distilled narrative
facts derived *alongside* verbatim turns through the product derive path
(`--ingest distill`) — improve LongMemEval-S accuracy over the published
raw baseline **88.0 ± 0.7** (three-judge triangulation band)?

**Verdict: inside the band, with a typed redistribution.** Distillation
buys preference accuracy and pays for it in knowledge-update accuracy,
net ≈ 0. The shape does NOT earn default-on in its naive (flat-facts)
form; the knowledge-update failure mode is specific and actionable:
**stale-fact poisoning** — a confidently dated fact atom asserting the
*old* state outweighs the later update at answer time. Fixing it requires
update-aware distillation (supersession semantics the product ledger
already has, which the flat A/B derive path deliberately did not use).

## Protocol

Config identical to the published run (12288-token packets, retrieval
limit 64, rank packing, semantic-local + reranker-local, Sonnet runner and
verbatim Appendix-A.4 judge via local Claude Code). Distiller: Haiku, 2-5
self-contained dated narrative facts per session (prompt in
`benchmarks/runner_claude_code.py`), content-addressed disk cache warmed
in parallel by `benchmarks/prewarm_distill.py` (6,573 sessions distilled
across the three stages, 0 failures, 26 empty fact lists = 0.4%).
Comparisons are **paired per question** against the persisted rows of the
signed 500-question raw run (same question ids, same judge protocol).

Cost note: the full-500 distill arm was deliberately not run. Staged
probes below bound the expected full-run delta at ≈ −0.6pp
(+6.7pp × 30/500 preference − 6.4pp × 78/500 knowledge-update, other
types flat on the stratified 60) — an expected ≈ 87.4%, inside the band;
~20 GPU-free but ~day-long subscription hours would not change the
decision.

## Stage 1 — stratified 60 (10 per type)

`benchmarks/results/distill-ab-60/` (signed). Overall **52/60 = 86.7%**,
identical to the paired raw score on the same 60 questions (52/60) and to
the raw tuned-prompt ablation reference (86.7%). Tokens/question 11,546
vs 11,668 raw — the packet budget binds either way. Per type
(distill vs paired raw): preference 8/10 vs 7/10, knowledge-update 7/10
vs 9/10, ssu 10/10 vs 9/10, everything else identical. Flips: 3 up
(2 preference, 1 ssu), 3 down (2 knowledge-update, 1 preference).

## Probe A — all 30 preference questions

`benchmarks/results/distill-ab-preference/` (signed). **20/30 = 66.7% vs
raw 18/30 = 60.0%** — paired flips 3 up / 1 down, net +2. Direction
consistent with stage 1: distilled facts give the reader synthesized
taste evidence that turn fragments scatter. Caveats: n=30, CI95 ±16.9pp,
and preference is the judge-sensitive type (60.0–76.7% across the three
judges on the raw run) — treat as a directional signal, not a headline.

## Probe B — all 78 knowledge-update questions

`benchmarks/results/distill-ab-knowledge-update/` (signed).
**70/78 = 89.7% vs raw 75/78 = 96.2%** — paired flips 0 up / 5 down.
Every one of the five is the same failure shape, verified against the
answers:

| qid | gold (updated) | distill answer (stale) |
|---|---|---|
| 6a1eabeb | 5K best 25:50 | 27:12 (earlier session's value) |
| 830ce83f | Rachel → suburbs | Chicago (pre-update move) |
| 2698e78f | therapist weekly | "every two weeks" (old cadence) |
| 69fee5aa | 38 coins | 37 (count before the update) |
| 031748ae_abs | (abstain) | answers from a stale role fact |

Several answers even *flag* the later contradicting session and still
lead with the stale value — the dated fact atom reads as more
authoritative than the raw update turn. This is the mechanism, not
retrieval noise: gold-session coverage stayed ~100%.

## Decision (per TODO item 1 rule)

Inside the band → the distill shape **stays opt-in**; the question moves
to extraction quality (TODO item 6) with a sharper target than "close the
gap": **distillation must be update-aware**. Concretely, a fact derived
from session N that a session N+k contradicts must be superseded or
validity-bounded (`valid_to`), not left competing — the product ledger's
supersession machinery exists precisely for this; the naive flat derive
path is what poisons knowledge-update. Preference upside (+6.7pp on its
type) is real but small in headline terms (+0.4pp), and is not worth the
knowledge-update regression until supersession-aware distillation lands.

Artifacts: three signed reports + per-question JSONL in the directories
above; verify with
`python -m joiny_mnemonic.report_signing verify <dir>/longmemeval-latest.json`.

---

# Stage: update-aware supersession (`--ingest distill-aware`, 2026-07-16)

**Mechanism.** Deterministic, no ingest LLM: a later fact that
near-duplicates an earlier one (content-token containment ≥ 0.30, dates
strictly ordered) supersedes it through the product ledger, dropping the
stale assertion from retrieval while history keeps it. Threshold
calibrated on the distill cache: the 5K value-update pair scores 0.333
vs background p99 0.143 / max 0.219 over 2,788 random cross-session
pairs.

**Pre-registered prediction** (from the failure census below): +1..2 of
the 5 KU losses; the rest is out of mechanism reach.

**Failure census of the 5 flat-distill KU losses** (verified against the
cache and raw transcripts): 1 clean value-update (5K — catchable), 1
update present but in a low-overlap fact (Rachel, containment 0.207 —
inseparable from background noise by tokens), 1 update never distilled
(therapist frequency — distiller recall), 1 requiring enumeration
(coins 37+1 — supersession would *hide* the base count), 1 poisoned
abstention (contradictory confident facts).

**Result: 69/78 = 88.5% vs flat 70/78 — net −1, within run-to-run noise.**
Paired flips vs flat: +2 (the predicted 5K fix and one more value-update,
01493427), −3, of which one question had *zero* supersessions fired
(618f13b2 — identical store, pure sampling noise; the frozen-config
variance band is ±2.5pp) and one is a verified false supersession
(59524333: a sports-schedule fact hidden behind a topically-similar
later fact at 0.38).

**In-vivo false-fire scale:** 170 supersessions across the 78 KU
questions (2.2/question, 1.2% of facts) against ~2 true updates. The
random-pair calibration did not transfer: real session streams have
recurring-topic structure (productivity apps re-asked scores 0.61), and
token containment cannot distinguish "same subject, updated value" from
"same topic, revisited" — the exact discrimination an update detector
needs.

**Conclusion (superseded by stage 3 below).** Ingest-side deterministic supersession by token overlap is insufficient
for the update-aware cell: it fixes the pure value-update class (2/78)
and pays comparable collateral. Combined with the census (2/5 of the
original KU damage is not update-shaped at all), the KU regression of
distilled facts is dominated by (a) distiller recall on updates and
(b) confident summaries suppressing enumeration/abstention at answer
time. Viable next shapes, in rising cost: answer-time recency/validity
discipline over near-duplicate facts (retrieval/packing side); write-time
LLM reconciliation (the Letta/Mem0 shape — one contradiction check per
colliding fact, costs ingest calls); entity-slot keying. None ships by
default without beating this measured bar. Artifacts:
`distill-aware-knowledge-update/` (signed), mechanism in
`longmemeval.py::_superseded_fact` behind `--ingest distill-aware`.

---

# Stage 3: keyed distillation (`--ingest distill-keyed`, 2026-07-16)

**Shape.** The classic pre-AI answer (SCD Type 2 / bitemporal tables;
the same shape Zep/Graphiti uses for agent memory): closure is
deterministic BY KEY, and the only genuinely hard step — turning free
text into keys — is done by the already-paid distillation call. The
keyed distill prompt (`LME_DISTILL_KEYED=1`, dedicated cache) emits
`{fact, key}` where key is `subject|attribute` for stateful facts
(`user|5k-personal-best`, `rachel|residence`) and null for one-off
events; at ingest a later fact with the same normalized key supersedes
the earlier one through the ledger. Live smoke: both 5K sessions,
distilled independently, emitted the byte-identical key. The keyed
prompt also instructs the distiller to state updated values explicitly
(a recall fix), so the arm changes two variables; a control isolates
them.

**Results, all 78 knowledge-update questions, paired (signed reports):**

| arm | KU accuracy | dirs |
|---|---|---|
| raw (baseline) | 75/78 = 96.2% | `longmemeval-latest.jsonl` |
| flat distill | 70/78 = 89.7% | `distill-ab-knowledge-update/` |
| token-overlap supersession | 69/78 = 88.5% | `distill-aware-knowledge-update/` |
| keyed facts, no closure (control) | 72/78 = 92.3% | `distill-keyedfacts-noclosure-ku/` |
| keyed facts + SCD closure | **73/78 = 93.6%** | `distill-keyed-knowledge-update/` |

**Decomposition.** The update-recall prompt alone fixes 3 of the 5
census losses (Rachel, therapist, coins — the update now exists as an
explicit fact and the reader prefers it unaided). Closure adds the pure
value-update class (5K — the stale fact must actually leave the view)
plus one poisoned-abstention case (f685340e_abs: removing contradictory
stale facts restored abstention), at one cost case (01493427). Known
mechanism pitfall, verified on the gym question (59524333): closure can
hide a *specific* fact ("gym at 7:00 PM Mon/Wed/Fri") behind a *vaguer*
later fact with the same key ("recurring gym commitments Mon/Wed/Fri")
— LLM facts do not guarantee the new version carries the full state.
Product implication: prefer validity-bounding (`valid_to`, record stays
retrievable for historical queries) over hard supersession, and/or
require the superseding fact to assert a value.

**Remaining gap to raw** (−2.6pp ≈ 2 questions): the
contradictory-confident-facts abstention case (031748ae_abs, out of
scope for any update mechanism) and run noise. Preference-side check of
the keyed prompt not yet run (keyed cache covers KU sessions only).
