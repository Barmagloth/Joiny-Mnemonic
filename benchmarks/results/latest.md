# Joiny-Mnemonic performance and retention benchmark

Token counter: `conservative-byte-word-estimate`. Exact for selected model: `False`.

| Workload | Raw tokens | Emitted | Saved | Critical recall | Path refs | Line recall | Exact recovery | Reducer p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| project-test-suite | 2746 | 120 | 2626 | 100.0% | 100.0% | 1.9% | yes | 7.383 |
| controlled-failing-suite | 9927 | 413 | 9514 | 100.0% | 100.0% | 3.2% | yes | 25.269 |
| real-source-search | 12421 | 5063 | 7358 | 100.0% | 100.0% | 0.0% | yes | 13.643 |
| real-git-diff-no-index | 246 | 246 | 0 | 100.0% | 100.0% | 100.0% | yes | 0.385 |

## Aggregate

- Token savings per exposure: **19498 (76.9%)**.
- Token savings at 10 exposures: **194980**.
- Reducer latency p95: **21.445 ms**.
- Enriched ingest latency p95: **29.476 ms**.
- Hook counter committed-append latency p95: **0.828 ms**.
- SQLite storage overhead: **222336 bytes**.
- Critical signal recall: **100.0%**.
- Exact source recovery: **100.0%**.
- Path/line reference recall: **100.0%**.
- Immediate verbatim line recall: **3.0%**.

## Gates

- PASS: `positive_net_token_gain`
- PASS: `critical_signal_recall_100pct`
- PASS: `exact_source_recovery_100pct`
- PASS: `path_reference_recall_100pct`
- PASS: `reducer_p95_under_50ms`
- PASS: `enriched_ingest_p95_under_100ms`
- PASS: `hook_counter_p95_under_25ms`
- PASS: `hook_counter_cumulative_exact`
- PASS: `hook_counter_replay_idempotent`
- PASS: `no_workload_expands_prompt`

Verbatim line recall is diagnostic, not a pass gate: compact views intentionally omit repetitive success lines. Exact immutable source recovery is the losslessness gate.
