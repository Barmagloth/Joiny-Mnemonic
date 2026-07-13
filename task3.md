# Task 3: bitemporal Phase A implementation hardening

## Purpose

Implement the deterministic, append-only bitemporal core specified by `task2.md`. This task covers schema, canonical normalization, branch-safe retrieval and compatibility only. It does not enable automatic extraction, introduce a knowledge graph, or change the default resume behaviour.

## Non-negotiable invariants

1. Transaction and valid time are independent. `created_at` remains wall-clock transaction time; `seq` is the canonical total order.
2. `known_at=K` is branch-local: resolve it in the requested branch lineage to the greatest visible `seq` with `created_at <= K`; replay through that cutoff only. An ancestor is visible only through its fork cutoff. Equal timestamps tie-break by `seq`; non-monotonic clocks must remain deterministic.
3. Store `valid_from`, `valid_to`, `valid_from_precision`, and `valid_to_precision`. Precision enum is `instant | day | month | year | unknown`; `interval` is not a precision.
4. Normalize relative dates in the source-event timezone when recorded, otherwise UTC. A day denotes that complete day in the resolution timezone. Remaining ambiguity is lowered in precision or quarantined.
5. Existing facts and APIs remain transaction-time compatible when no temporal option is supplied.
6. `current=true` returns only records proven valid now. Unknown-validity records are returned only through an explicit separate partition/option and every hit has `validity_status` before prompt rendering.
7. Candidate identity includes normalized bounds and both precisions. A temporal collision is retained or explicitly append-only rejected with `rejection_reason`; it is never silently lost.
8. Temporal conflicts and effective intervals are derived projections only. They do not mutate history rows. Projection output used in retrieval/prompting exposes a `temporal_projection_code_version` and follows snapshot/replay versioning rules.
9. Temporal metadata never raises authority, confirms a candidate, or alters trusted memory without the existing evidence and policy path.
10. Resume remains transaction-time by default. A valid-now mode is opt-in; changing the default is out of scope.

## Scope

### Schema and migrations

- Add nullable temporal fields to the appropriate append-only record/candidate tables and supporting indexes.
- Extend the immutable migration mechanism; preserve existing databases and historical snapshots.
- Do not rewrite events, memories, candidates, or snapshots as a migration shortcut.
- Update canonical materialized-state serialization, state hashes, and replay metadata where temporal state participates.

### Deterministic normalization

- Accept exact timestamps, dates, month/year values, bounded and open intervals, and evidence-anchored relative expressions.
- Preserve `temporal_expression`, source event ID, and exact evidence span/offset.
- Route ambiguous, timezone-less, model-only, or evidence-zone-disallowed values to unknown/quarantine.
- Keep normalization independent of model confidence and trust elevation.

### Retrieval

- Add explicit `valid_at`, `known_at`, `current`, `include_unknown_validity`, and `history` controls consistently to service, CLI, MCP and HTTP surfaces.
- Return temporal bounds, independent precisions, observed time, validity status, provenance and projection version in hits.
- Preserve old input shapes and old output semantics unless an explicit temporal option is used.
- Keep temporal conflict detection and effective intervals in the materialized/retrieval projection, with optional append-only findings only.

## Tests required before handoff

- Legacy database migration and legacy caller compatibility.
- Instant/day/month/year/open interval handling, including start `month` with unknown end precision.
- Source timezone and UTC fallback for relative dates; ambiguous day boundary quarantine/downgrade.
- `valid_at` boundaries under `[from, to)` semantics.
- `known_at` at a fork cutoff, and events whose `created_at` order differs from `seq`.
- Proven-current and separately partitioned unknown-validity results.
- Candidate temporal collision preservation or explicit rejection.
- No untrusted temporal evidence can confirm, supersede or mutate trusted memory.
- Deterministic full replay, snapshot hash verification, and projection-version visibility.
- Resume output unchanged without an explicit valid-now option.

## Definition of done

All tests pass, the full existing suite passes, `git diff --check` is clean, documentation reflects the public controls, and automatic extraction remains disabled by default.