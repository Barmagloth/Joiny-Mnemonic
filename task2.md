# Task 2: append-only temporal reasoning

## Status

Proposed follow-up. This task is independent of automatic-extraction enablement and does not relax any trust, provenance, quarantine or evidence requirements from `task.md`.

## Problem

`created_at` records when the store learned evidence. It does not say when an asserted fact was true. The system therefore needs bitemporal retrieval without weakening append-only history or trust rules.

## Required semantics

Keep two independent time dimensions:

1. Transaction time is the event or memory-version admission time. `created_at` remains its wall-clock representation; the canonical total order is `seq`, not the clock.
2. Valid time is when the assertion applies in the represented world. Add nullable `valid_from` and `valid_to`, using `[valid_from, valid_to)`.

Retain `observed_at`, the exact `temporal_expression`, source event IDs and evidence spans. Store independent boundary precision fields:

- `valid_from_precision`: `instant | day | month | year | unknown`;
- `valid_to_precision`: `instant | day | month | year | unknown`.

`interval` is a shape, not a precision. An absent bound means unknown/open, never an assertion of infinity. Normalized values are timezone-aware ISO 8601.

Relative dates are resolved in the source-event timezone when that timezone is recorded; otherwise UTC. A day-precision value denotes the complete calendar day in that resolution timezone. A boundary that remains ambiguous after this policy must be lowered in precision or quarantined. The original expression is always retained.

## Append-only representation

Memory history remains immutable.

- Temporal fields are fixed when a version is appended.
- Corrections append a successor linked by `supersedes_id`; old rows are never updated.
- A materialized projection may derive an effective interval from successors, but source rows remain byte-for-byte unchanged.
- Retroactive corrections remain visible through transaction-time queries.
- Every derivation is reproducible from canonical events.

Start with nullable temporal columns on `memory_records` and `extraction_candidates`. Candidate uniqueness must include normalized `valid_from`, `valid_to`, and both boundary precisions. If an otherwise identical candidate conflicts only in temporal data, retain a second append-only candidate with an explicit rejection reason; never silently drop it.

## Evidence and trust rules

Temporal metadata inherits the authority of its underlying memory and never raises it.

- Normalize only explicit temporal evidence or permitted relative expressions.
- Ambiguous dates, missing timezone context, and model-only guesses stay unknown or quarantined.
- Inferred dates cannot confirm, supersede, or invalidate trusted memory.
- Code blocks, quotes, tool output, prompt injection, and private regions retain all existing evidence-zone restrictions.
- Extractor confidence is routing input, not proof of temporal validity.

## Retrieval contract

Add orthogonal controls:

- `valid_at=T`: facts whose valid interval contains `T`.
- `known_at=K`: in the requested branch lineage, select the greatest visible event `seq` with `created_at <= K`, then replay only events at or before that sequence. Ancestor events are visible only through each branch's fork cutoff. When equal wall-clock timestamps occur, `seq` is the documented tie-breaker. This prevents knowledge from leaking across forks and makes boundary calls deterministic even when clocks move backwards.
- `current=true`: return only facts proven valid now. Unknown-validity records do not belong in this primary result.
- `include_unknown_validity=true`: return unknown-validity records in a separately labelled partition, with each hit carrying `validity_status` before prompt rendering.
- `history=true`: return superseded records and their transaction/valid time.
- `since` and `until`: transaction-time filters with existing semantics.

A hit includes stored bounds, both precisions, `temporal_expression`, `observed_at`, `validity_status`, and the effective interval when projected. The projection implementation has a version identifier (`temporal_projection_code_version`) in hit metadata and shares the versioning discipline of snapshot replay, because it affects prompt content and exposure audit.

For MVP, resume packets remain transaction-time by default and therefore preserve existing caller behaviour. A valid-now resume mode is opt-in. Changing that default requires a separate decision after metrics.

## Conflicts

Temporal conflicts are derived in a materialized projection: equal normalized content or relation keys with overlapping effective intervals and incompatible values are marked as temporal conflicts. A finding may report that projection, but no conflict status is written back to a memory-history row and no mutation is introduced.

## Interfaces and observability

Extend MCP and HTTP retrieval with the controls above. Existing calls without temporal controls preserve current transaction-time results. Extend capabilities with temporal support and projection version. Record temporal normalization, quarantine, and conflict findings in the existing append-only audit pipeline without exposing private source text.

## Snapshots and replay

Temporal columns and projection inputs participate in canonical materialized state. Snapshot/replay metadata must carry the materialization code version and the temporal projection version where applicable; legacy replay remains readable. Rebuilds must verify canonical state hashes exactly as the snapshot hardening rules require.

## Delivery phases

### Phase A: schema and deterministic normalization

- Add nullable temporal fields and indexes without rewriting history.
- Implement canonical parsing, timezone policy, precision handling, and quarantine.
- Preserve legacy retrieval behaviour.

### Phase B: extraction and consolidation

- Permit extraction candidates to carry temporal evidence and both precisions.
- Enforce temporal candidate collision handling.
- Materialize effective intervals and derived conflicts without row mutation.

### Phase C: retrieval and evaluation

- Add temporal query controls and explicitly partition unknown validity.
- Add opt-in valid-now resume mode; do not change the default.
- Measure false trusted temporal records, quarantine rate, retrieval accuracy, replay determinism, and resume impact before any broader enablement.

## Required tests

- Explicit instant, day, month, year, open-ended, and intervals with different start/end precision.
- Relative dates resolved in recorded timezone and UTC fallback; ambiguous timezone boundaries are lowered or quarantined.
- Append-only correction and retroactive-validity cases.
- `valid_at`, valid-now, history, and separately partitioned unknown-validity retrieval.
- `known_at` at a branch fork cutoff and two events whose wall-clock order disagrees with `seq`.
- No temporal metadata from untrusted evidence can confirm or mutate trusted memory.
- Candidate collisions differing only in temporal fields are retained or explicitly rejected, never lost.
- Snapshot/replay and temporal projection versions remain deterministic and hash-verifiable.
- Resume behaviour is unchanged by default.

## Acceptance criteria

1. Existing callers without temporal controls keep transaction-time results.
2. Valid-time results are deterministic, provenance-bound, and branch-lineage-safe.
3. `known_at` is deterministic at equal or non-monotonic wall-clock values through canonical `seq` ordering.
4. Unknown validity cannot be consumed as current truth through the primary result set.
5. Temporal conflicts are derived projections, never mutations of memory history.
6. Relative-date normalization has an explicit timezone policy and preserves source expressions.
7. Candidate temporal collisions cannot silently discard data.
8. Full replay, snapshots, and retrieval projection remain versioned and hash-verifiable.
9. Resume behaviour remains unchanged by default.