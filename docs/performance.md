# Performance and information-retention evaluation

The benchmark answers three separate questions. They must not be collapsed into a single
"compression ratio" number.

1. Does the prompt representation use fewer tokens after its provenance frame is included?
2. What CPU, ingestion-latency and SQLite-storage overhead is added?
3. Which information remains immediately visible, and can omitted information be recovered
   exactly from canonical storage?

## Workloads

`joiny-mnemonic-benchmark` executes commands and captures their real stdout/stderr:

- the repository's complete `unittest` suite;
- a controlled 401-test suite containing one real assertion failure;
- `rg -n "def |class " src/joiny_mnemonic tests` over the live checkout;
- `git diff --no-index` over two real files.

The failure workload is controlled fault injection: it verifies failure name, traceback,
assertion values, file/line and final counts. It is not a fabricated output string. Additional
production captures can be supplied by calling `run_benchmark(..., workloads=...)` with commands
that are safe in the target environment.

Commands are executed once per report. The deterministic reducer is then repeated (100 times by
default) over the captured output for stable latency percentiles. Raw command runtime is reported
but excluded from reducer overhead.

## Compared paths

Two fresh SQLite stores are initialized with the same schema.

- Baseline appends canonical raw tool output only.
- Enriched appends the identical canonical raw output, creates derived views and records usage.

The report compares committed ingest latency and database size after a full checkpoint. Exact
recovery reads the source through the public promotion path and compares SHA-256 digests.

## Retention metrics

- `critical_signal_recall`: failures, errors, test summaries and command-specific critical data.
- `path_reference_recall`: unique `path:line` references retained in a search index view.
- `verbatim_line_recall`: exact non-empty source lines still present immediately. This is expected
  to be low for verbose successful-test output and is reported, not hidden.
- `exact_source_recovery_rate`: byte-identical canonical output recovered through source promotion.

The product gate requires 100% critical-signal recall, 100% path-reference recall and 100% exact
source recovery. Verbatim-line recall is not a pass gate because progressive disclosure
intentionally removes repetitive lines from the immediate prompt; the exact source remains the
losslessness boundary.

## Profit metrics and gates

The view frame and source pointer are included in emitted-token counts. A view is never stored for
prompt use if it is larger than the raw representation.

Default gates:

- positive aggregate token gain;
- no individual workload expands the prompt;
- 100% critical-signal recall;
- 100% path-reference recall;
- 100% exact-source recovery;
- reducer p95 below 50 ms;
- enriched committed-ingest p95 below 100 ms.
- atomic hook-counter committed-append p95 below 25 ms;
- exact cumulative value and replay idempotence for the hook counter.

CPU milliseconds and provider tokens are reported separately. They are not converted into each
other without a supplied deployment-specific price model. `tokens_saved_per_reducer_ms`, storage
overhead, and storage bytes per saved token make the trade-off visible.

## Tokenizer authority

With the optional `tiktoken` dependency, the report uses the selected OpenAI encoding and records
its name. Without it, the existing conservative byte/word estimator is used and
`exact_for_model=false` is written to the report. Claude tokenization is not claimed to be exact.
Provider-reported hook usage is stored separately with `estimated=false` and is authoritative for
production cost reports.

## Run

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"
python benchmarks/run_benchmark.py --project-root . `
  --repetitions 100 --prompt-exposures 10 --assert-gates
```

Machine-readable and Markdown reports are written to `benchmarks/results/latest.json` and
`benchmarks/results/latest.md`.

## Extraction storage amplification

Storage benchmarks must report four categories separately: canonical tables, immutable
interpretation-ledger tables, optional compressed raw extractor payloads, and rebuildable
projections/indexes. A single total database size is insufficient for retention planning.

Runtime telemetry reports pending events, oldest pending age, failed events, retry count and last
success. Worker concurrency is bounded by configuration. At-least-once inference means a crash
may repeat model execution; uniqueness plus the atomic success transaction provides exactly-once
durable candidate/memory effects for an event and configuration hash.


## Hook-path timing (task6A)

`joiny-mnemonic-hook-timing` measures what a host pays per hook delivery,
per scenario, in two modes (core only / installed plugins), and asserts
loose p95 budgets as gates — order-of-magnitude tripwires against silent
hot-path regressions, not micro-benchmarks:

    joiny-mnemonic-hook-timing --project-root . --repetitions 30 --assert-gates

Reference points (2026-07-15, development machine, warm process): capture
~20ms p50; reducer path p50 ~52ms with a variance-prone tail (~390ms p95);
resume injection ~330ms; compact path ~390ms; reconciler passes 3-5ms.
Installed plugins (semantic + reranker) cost ~20ms extra at p95 on warm
paths; the first semantic search of a process additionally pays one-time
model load. The capture path is guarded by a cold-feature invariant test:
hook delivery must never import heavyweight optional dependencies
(torch/sentence-transformers stay lazy until a retrieval surface runs).
The stamped report lands in `benchmarks/results/hook-timing-latest.json`.
