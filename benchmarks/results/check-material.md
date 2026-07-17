# CHECK MATERIAL probe — closed as an accuracy experiment (2026-07-17)

**Idea** (user, 2026-07-16): an opt-in packing section that surfaces
verification material — earlier superseded versions of facts and
still-live near-duplicate conflicts — instead of resolving conflicts
silently. Target: the two failure classes no ingest mechanism reached
(poisoned abstention, vague-successor information loss).

**Pre-registered format** (user, 2026-07-17): one experimental packing
flag, no new subsystem, eval-only; decisive signal = mechanism-audited
flips on the abstention-30 probe, NOT headline percentages (residual
gaps are 1-2 questions, below the ±2.5pp run noise); preference-30
guard = no mechanism-confirmed regression; stratified-60 keyed pair as
a hidden-regression scan; token composition and build latency recorded
(6A discipline).

**Implementation**: `--check-material` in the harness; the section is
built from the candidate pool BEFORE packing and reserves exactly its
own size. All arms on `distill-keyed` ingest + keyed cache; signed
reports in `cm-*/` directories.

## Results

| probe | baseline | +CHECK MATERIAL |
|---|---|---|
| abstention-30 (decisive) | 27/30 | 27/30 (v1), 26/30 (v2) |
| preference-30 (guard) | 21/30 | 19/30 (v1, bug), **23/30 (v2)** |
| stratified-60 (scan) | 52/60 | 52/60 |

Mechanism audit:

- **Abstention:** no reproducible mechanism flips. v1's one up-flip
  (a96c20ee_abs, verification-style answer) did not reproduce in v2;
  every down-flip across both versions had `cm_tokens=0` (identical or
  near-identical packets — sampling noise, not section content). The
  designed-for target case (031748ae_abs) was genuinely REACHED — the
  section surfaced the 4-vs-5-engineers conflict and the answer reasons
  about both values — but the model reconciled them plausibly instead
  of abstaining, and the question's actual trap is a role-title
  mismatch, out of reach of any fact-conflict surface.
- **Preference v1 regression root-caused and fixed:** the entire
  21→19 drop came from a fixed 600-token reserve displacing evidence
  in questions where the section never fired (fired 1/30 on
  preference; all six down-flips had cm_tokens=0 and 300-1000 fewer
  context tokens). After building the section pre-packing and
  reserving only its actual size, the guard passed (23/30). The
  empty-section-zero-cost pattern is the lasting lesson for ANY future
  packet section.
- **60 pair:** 2 up / 2 down, half with cm_tokens=0 — noise-shaped
  churn, net zero. Per-type keyed baseline showed no hidden keyed-form
  regressions either (temporal 10/10, multi-session 7/10 = flat = raw;
  KU 9/10 vs flat 7/10).

Costs (measured): section fires in 3% (preference) to 43% (abstention)
of questions; ~220 tokens when it fires; build latency p95 < 5ms.

## Verdict (per the pre-registered rule)

No reproducible mechanism-attributable gains on the decisive probe →
**closed as an accuracy experiment on LongMemEval**. The flag stays in
the harness as a measured option; nothing ships.

Scope note: this verdict is about benchmark accuracy only. The product
idea — a verify-mode packet section carrying exact sources, relevant
failures/lessons and stale/precheck warnings for *auditability* — is a
UX/trust question this benchmark cannot measure, and it remains open
with one design constraint inherited from this probe: any such section
must cost zero evidence when empty.
