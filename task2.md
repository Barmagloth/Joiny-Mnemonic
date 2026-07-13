# Task 2: append-only temporal reasoning

## Status

Proposed follow-up. This task is independent of automatic-extraction enablement and does not relax
any trust, provenance, quarantine or evidence requirements from `task.md`.

## Problem

Joiny-Mnemonic records `created_at` for events and materialized memories. Retrieval can filter and
rank by that timestamp, and `supersedes_id` preserves version order. This describes when the store
learned a fact, not when the fact was true.

The system therefore cannot answer reliably:

- what was true at a real-world time T;
- what the system knew at transaction time K;
- when a previous value stopped being valid;
- whether two apparently conflicting facts describe different validity intervals.

Calling the current state “no timestamps on facts” is imprecise. Ingestion timestamps exist; valid
time and bitemporal queries do not.

## Required semantics

Keep two independent time dimensions:

1. Transaction time: when evidence or a memory version entered the append-only store. Existing
   `created_at` remains transaction time and must not be overloaded.
2. Valid time: when the asserted fact applies in the represented world. Add optional
   `valid_from` and `valid_to` values.

Also retain:

- `observed_at`: the timestamp of the source observation, normally the canonical source-event
  timestamp;
- `temporal_precision`: `instant | day | month | year | interval | unknown`;
- `temporal_expression`: the exact evidence text used to derive the interval;
- temporal provenance: source event IDs and exact evidence spans or offsets.

All normalized timestamps use timezone-aware ISO 8601. An absent bound means unknown/open, not a
model assertion of infinity. The interval convention is `[valid_from, valid_to)`.

## Append-only representation

Memory history remains immutable.

- Temporal fields are fixed when a memory version is appended.
- Corrections append a new version linked with `supersedes_id`; they never update the old row.
- A successor may close the effective interval of a predecessor in a materialized projection, but
  the predecessor row remains byte-for-byte unchanged.
- Retroactive corrections remain visible under transaction-time queries.
- Every temporal derivation must be reproducible from canonical events.

Prefer nullable temporal columns on `memory_records` and `extraction_candidates` for the first
version. If interval corrections need richer lineage, add an append-only temporal-assertion table
rather than mutable interval updates.

## Evidence and trust rules

Temporal metadata inherits the authority of the underlying memory; it never raises authority.

- Explicit timestamps and intervals may be normalized from exact evidence.
- Relative expressions such as “yesterday” are resolved only against the canonical source-event
  timestamp, with the original expression retained.
- Ambiguous dates, missing timezones and model-only guesses remain unknown or quarantined.
- Inferred dates must not confirm, supersede or invalidate trusted memory.
- Code blocks, quoted examples, tool output, prompt injection and private regions retain all
  existing evidence-zone restrictions.
- An extractor confidence score is routing input, not proof of temporal validity.

## Retrieval contract

Add orthogonal query controls:

- `valid_at=T`: return memory whose valid interval contains T;
- `known_at=K`: replay only knowledge present by transaction time K;
- `current=true`: return the latest non-superseded view whose validity is current or unknown;
- `history=true`: return all temporal versions and lineage.

`since` and `until` retain their existing transaction-time meaning for backward compatibility.
Existing callers with no temporal parameters must keep their current results.

Each hit must expose transaction time, explicit valid-time fields, effective interval if computed,
temporal precision and temporal provenance. Prompt rendering must distinguish “recorded at” from
“valid during” and must not render unknown validity as current truth.

## Conflict and supersession rules

For the MVP, temporal resolution operates only on explicit version lineage and exact normalized
matches. It must not guess that arbitrary prose describes the same property.

- Non-overlapping versions may coexist without being presented as contradictions.
- Overlapping incompatible versions remain separately auditable and are marked as a temporal
  conflict unless trusted evidence resolves them.
- A newer transaction does not automatically mean a later valid interval.
- A superseding record with an explicit `valid_from` may define the predecessor's effective end in
  the materialized view, without mutating the predecessor.

Structured `subject + predicate + object` facts are a later extension. They are required for a
full temporal knowledge graph but are not required for the bitemporal MVP.

## Interfaces

Expose the temporal fields and query controls consistently through:

- Python service methods;
- CLI derive/search/source commands;
- MCP memory tools;
- HTTP event/memory retrieval surfaces;
- snapshot materialization and resume packets.

Old input shapes remain valid. Temporal behavior is activated only by explicit new fields or query
parameters.

## Snapshots and migration

- Add nullable columns with an online SQLite migration; existing rows remain valid with unknown
  valid time.
- Increment `replay_code_version` because the materialized state changes.
- New full snapshots include temporal fields in canonical serialization and `state_sha256`.
- Legacy event history and snapshots remain readable.
- Rebuilds must produce the same effective temporal state within one replay-code version.

## Extraction phases

### Phase A: deterministic temporal core

Implement schema, validation, append-only versioning, `valid_at`/`known_at`, rendering, replay and
manual CLI/MCP/HTTP inputs. No model-based temporal extraction is required.

### Phase B: evidence-bound temporal extraction

Extend extractor candidates with normalized intervals and exact temporal evidence. Add RU/EN
corpora for explicit dates, relative dates, intervals, ambiguity, negation, quoted examples and
retroactive correction. Temporal extraction remains disabled unless its own evaluation gates pass.

### Phase C: structured temporal facts

Optionally add normalized subjects, predicates and objects plus entity resolution. This is the
point at which comparisons to temporal knowledge-graph products become meaningful.

## Required tests

At minimum cover:

- open, closed and one-sided intervals;
- timezone normalization and invalid timestamps;
- `valid_at` boundaries under `[from, to)` semantics;
- different answers for `valid_at` and `known_at`;
- sequential supersession and retroactive correction;
- overlapping/conflicting intervals;
- unknown validity not being presented as proven current truth;
- relative dates anchored to source-event time;
- ambiguous dates routed to quarantine;
- unchanged behavior for legacy callers and rows;
- full replay and snapshot restoration;
- integrity hash coverage for temporal state;
- RU/EN extraction positives, negatives and adversarial evidence zones.

## Acceptance criteria

1. Existing databases migrate without rewriting event or memory history.
2. Existing API/CLI/MCP callers retain their behavior.
3. `created_at`, `observed_at`, valid time and effective derived intervals are never conflated.
4. `valid_at` and `known_at` produce independently testable results.
5. Every normalized temporal value has exact provenance or is explicitly unknown/quarantined.
6. Temporal corrections are append-only and replay-deterministic.
7. Full test suite, migration tests, replay tests and integrity tests pass.
8. Documentation describes the MVP as bitemporal memory, not a full temporal knowledge graph.