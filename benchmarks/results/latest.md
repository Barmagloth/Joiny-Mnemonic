# Joiny-Mnemonic performance and retention benchmark

Token counter: `conservative-byte-word-estimate`. Exact for selected model: `False`.

| Workload | Raw tokens | Emitted | Saved | Critical recall | Path refs | Line recall | Exact recovery | Reducer p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| project-test-suite | 12361 | 472 | 11889 | 100.0% | 100.0% | 3.3% | yes | 42.617 |
| controlled-failing-suite | 9928 | 414 | 9514 | 100.0% | 100.0% | 3.2% | yes | 31.664 |
| real-git-diff-no-index | 161 | 161 | 0 | 100.0% | 100.0% | 100.0% | yes | 0.336 |

## Aggregate

- Token savings per exposure: **21403 (95.3%)**.
- Token savings at 10 exposures: **214030**.
- Reducer latency p95: **35.663 ms**.
- Enriched ingest latency p95: **42.231 ms**.
- Hook counter committed-append latency p95: **0.819 ms**.
- SQLite storage overhead: **210072 bytes**.
- Storage classes: canonical_data=68715 bytes, interpretation_ledger=1450 bytes, raw_extractor_payloads=0 bytes, rebuildable_projections=72818 bytes, database_file_bytes=2879880 bytes.
- Critical signal recall: **100.0%**.
- Exact source recovery: **100.0%**.
- Path/line reference recall: **100.0%**.
- Immediate verbatim line recall: **4.8%**.

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
