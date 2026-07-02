# Joiny-Mnemonic performance and retention benchmark

Token counter: `conservative-byte-word-estimate`. Exact for selected model: `False`.

| Workload | Raw tokens | Emitted | Saved | Critical recall | Path refs | Line recall | Exact recovery | Reducer p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| project-test-suite | 2632 | 120 | 2512 | 100.0% | 100.0% | 2.0% | yes | 7.827 |
| controlled-failing-suite | 9935 | 421 | 9514 | 100.0% | 100.0% | 3.2% | yes | 23.279 |
| real-source-search | 12341 | 5034 | 7307 | 100.0% | 100.0% | 0.0% | yes | 13.606 |
| real-git-diff-no-index | 261 | 261 | 0 | 100.0% | 100.0% | 100.0% | yes | 0.387 |

## Aggregate

- Token savings per exposure: **19333 (76.8%)**.
- Token savings at 10 exposures: **193330**.
- Reducer latency p95: **18.364 ms**.
- Enriched ingest latency p95: **29.025 ms**.
- Hook counter committed-append latency p95: **2.628 ms**.
- SQLite storage overhead: **24576 bytes**.
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
