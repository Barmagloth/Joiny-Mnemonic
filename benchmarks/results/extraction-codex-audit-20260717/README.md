# Independent Codex audit, 2026-07-17

This directory is a separate, non-destructive review of the saved extraction
gate artifacts. It does not replace or modify the original benchmark reports.

## Judge

- Local `codex exec`
- Model: `gpt-5.4`
- Sandbox: read-only
- Inputs: both v2 corpora, both saved JSONL outputs, the combined report, the
  gate runner, and the evaluator implementation
- Structured result: `audit.json`

## Verdict

`pass_with_narrowed_claim`

The saved data support strong bilingual **preference type-span** results on the
development corpus:

| Language | Precision | Recall |
|---|---:|---:|
| English | 0.981 | 1.00 |
| Russian | 0.962 | 1.00 |

The adversarial result is narrowly: **0 auto-trusted records from 6 declared
trap examples**. It is not `0/140`; one Russian blockquote trap still produced
a quarantined preference candidate.

The data do not support a broad claim that all memory types are classified at
0.96-0.98 precision. The non-preference block has only seven examples per
language, and English fact precision in the saved run is 0.25. Exact-triple
scores are also near zero, so the headline result measures type plus overlapping
evidence spans, not normalized-content or provenance formatting quality.

## Methodology status

Run 1 was the honest pre-iteration measurement and failed recall:

- English preference: precision 1.00, recall 0.78
- Russian preference: precision 0.97, recall 0.67

The prompt was then changed using misses from this same corpus. Therefore the
final 0.981/1.00 and 0.962/1.00 figures are **development-set results**, not a
production generalization gate.

The next valid protocol is:

1. Freeze and identify the prompt before authoring evaluation data.
2. Author an independent bilingual held-out tranche after the freeze.
3. Run repeated trials to expose model stochasticity and version drift.
4. Keep type-span and exact-content metrics separate.
5. Add enough fact/decision/task/failure/lesson positives before making
   cross-type claims.

Running the frozen prompt again on the current v2 corpus would only remeasure
the tuned development set. No such run was performed in this audit.
