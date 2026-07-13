# Task 4: bitemporal memory core — major version

## Status

**Phase A implemented** (temporal core, schema v8, derive path, bitemporal retrieval controls,
snapshot integration, CLI/MCP/HTTP surface, docs and traceability; full suite green), then
hardened after adversarial review: known-at recall through divergent-text supersession,
fail-closed cutoff handling for plugin event hits, validity_status anchored to *now* under
`valid_at`, calendar errors surfaced as `TemporalValidationError`, and a real pre-migration
schema-v7 fixture regression test. Accepted deviation from the letter of the compatibility
rule: temporal-parameter-free derive responses gain additive `null` temporal keys (search-hit
metadata stays byte-identical; pinned by the fixture test). Phase B (evidence-bound temporal
extraction) remains gated and unimplemented.

## Original specification

Consolidated implementation specification for the next major version. Supersedes `task2.md` and
`task3.md` as the single implementation reference; both remain in the repository as design
history. Incorporates the review corrections that surfaced after `task3.md` was written:
known-at-aware projection, open-ended validity semantics, `observed_at` definition, and the
removal of relation-key vocabulary from the MVP conflict rule.

This task does not enable automatic extraction, does not introduce a knowledge graph, and does
not change default resume behaviour. Temporal extraction (Phase B) is gated by its own
policy-ledger flag and its own evaluation corpus, exactly like `task.md` extraction.

## Versioning and compatibility contract

- This is a major version (current 0.8.x -> 1.0.0). "Major" reflects the scope of the new query
  semantics, not a compatibility break.
- Hard rule: **no existing caller changes behaviour unless it passes a new temporal option.**
  Every pre-1.0 API/CLI/MCP/HTTP call without temporal parameters must return byte-compatible
  results, verified by regression tests against a pre-migration fixture database.
- Schema changes are additive nullable columns plus indexes, applied through the existing
  immutable `schema_migrations` mechanism. No event, memory, candidate or snapshot row is
  rewritten. Legacy rows carry `NULL` temporal fields, meaning unknown validity.
- `replay_code_version` is incremented (materialized state gains temporal fields). A new
  `temporal_projection_code_version` identifies the projection code (see Invariant 3). Legacy
  snapshots remain readable under their recorded versions.

## Prior art and adopted conventions

Reviewed before design; adopt the conventions, not the dependencies:

- **SQL:2011 bitemporal tables** (system-versioned + application-time periods): two orthogonal
  axes with four bounds per row. Adopted: half-open `[from, to)` periods, separate axes never
  conflated, additive columns on existing tables.
- **XTDB**: every record bitemporal by default; as-of queries on either or both axes. Adopted:
  the combined `valid_at + known_at` query as the canonical operation; their "DIY bitemporality"
  writing documents that hand-rolled implementations usually break exactly on as-of-aware
  derived state — which is why Invariant 3 exists.
- **Graphiti/Zep**: four timestamps per fact edge (`valid_at`/`invalid_at`,
  `created_at`/`expired_at`). Rejected: Graphiti **mutates** edges in place when invalidating
  (`edge.invalid_at = ...; edge.expired_at = ...`). Joiny-Mnemonic never mutates history; the
  same effect is achieved by appending a successor version and deriving effective intervals in a
  versioned projection. This preserves the product differentiator: every temporal assertion is
  auditable to an immutable source.
- **Allen's interval algebra (1983)**: the 13 basic interval relations are the complete
  qualitative basis; all are expressible as endpoint comparisons over half-open intervals.
  Adopted: endpoint semantics as the ground truth. Rejected: the composition/transitivity
  calculus (constraint propagation over relation networks is NP-hard in general and unnecessary
  here — see "Temporal logic core", non-goals).
- **SQL:2011 period predicates** (`CONTAINS`, `OVERLAPS`, `EQUALS`, `PRECEDES`, `SUCCEEDS`,
  `IMMEDIATELY PRECEDES/SUCCEEDS`): the pragmatic, standard-named subset of Allen's relations.
  Adopted as the public predicate vocabulary.
- **TSQL2 indeterminate instants**: a bound with limited precision is an instant known only
  within an envelope. Adopted: precision-aware bounds evaluate to an earliest/latest envelope,
  and predicates over envelopes return three-valued results.
- **Kleene three-valued logic**: `TRUE | FALSE | UNKNOWN` with standard connectives. Adopted as
  the evaluation semantics — an unknown bound propagates `UNKNOWN`; it is never coerced to a
  boolean inside the core. This is the formal footing for the existing trust rule that unknown
  validity must not masquerade as current truth.
- **TOKI (arXiv 2606.06240)**: bitemporal operator algebra for contradiction resolution in
  LLM-agent memory. Adopted as background for the conflict projection; its operators are not
  implemented in the MVP.
- **datetime/stdlib only.** No temporal library dependency. ISO 8601 parsing and timezone
  handling via the standard library; the zero-dependency core is non-negotiable.

## Temporal logic core

One pure module (`temporal.py`, stdlib only, no I/O, no SQL) is the single source of temporal
truth. Every temporal decision anywhere in the product — `validity_status`, `valid_at`
filtering, effective intervals, conflict detection, prompt rendering guards — must be expressed
through this module's primitives. No other code may compare temporal values directly.

Primitives, from the bottom up:

1. **Bound** = `(timestamp | None, precision)`. A bound with precision expands to a half-open
   **envelope** `[earliest, latest)` (a `day` bound covers that calendar day in its resolution
   timezone; `None` expands to an unbounded side). This is the only representation; there is no
   separate "exact" code path — an `instant` bound is an envelope of zero width.
2. **Interval** = `[from_bound, to_bound)` over envelopes.
3. **Three-valued predicates** over intervals and points, named after SQL:2011:
   `contains(interval, point)`, `overlaps(a, b)`, `equals(a, b)`, `precedes(a, b)`,
   `succeeds(a, b)`, `meets(a, b)` — each returning `TRUE | FALSE | UNKNOWN` by envelope
   endpoint comparison (definite when envelopes cannot disagree, `UNKNOWN` otherwise), plus the
   Kleene connectives `and3 / or3 / not3`. The 13 Allen relations remain expressible through
   endpoint comparisons if a later phase needs the finer distinctions.
4. **As-of composition**: every predicate evaluation takes the `known_at` cutoff as context;
   inputs are the interval versions visible at that cutoff (Invariant 3 is enforced by this
   signature — full-history evaluation is unrepresentable).

Derived definitions (fixed compositions, no new comparison logic):

- `validity_status`: `current` = `contains(interval, now) is TRUE`; `current_open` =
  `precedes(from_envelope, now) is TRUE and to_bound is unknown`; `expired` / `not_yet_valid` =
  the respective `FALSE`-side definite results; `unknown` = everything else.
- Temporal conflict = `overlaps(a, b) is not FALSE` on incompatible same-lineage or
  exact-normalized-content versions — `UNKNOWN` overlap is surfaced as a *possible* conflict,
  never silently dropped and never asserted as definite.
- Effective interval closure = successor's `valid_from` envelope applied to the predecessor in
  the projection, evaluated under the same `known_at` context.

Non-goals of the core: no Allen composition tables, no constraint network or path-consistency
solving, no inference of relations *between* distinct facts, no calendar arithmetic beyond
envelope expansion. That is where temporal reasoning becomes a research project; the MVP core is
a finite, exhaustively testable algebra of endpoint comparisons.

Size and test discipline: the module is small enough (~200–300 LOC) for near-exhaustive
truth-table tests — every predicate over every combination of
known/unknown × precision × ordering of bounds — plus duality properties
(`precedes(a,b) == succeeds(b,a)`, `not3` involution). `temporal_projection_code_version`
identifies this module's semantics; any change to a truth table bumps it.

## Product-mechanism reuse rule

Everything around the temporal logic core builds on what exists; the feature must reuse, not
duplicate:

| Need | Reused mechanism |
|---|---|
| Version lineage | `supersedes_id` on memory versions |
| Evidence binding | source event IDs + evidence spans/offsets |
| Untrusted-input routing | quarantine + evidence zones from `task.md` |
| Derived-state versioning | snapshot `replay_code_version` discipline |
| Migration | immutable `schema_migrations` history |
| Audit of what the agent saw | existing prompt-exposure records |
| Enablement control | policy ledger (new flag for Phase B only) |

Explicitly **not built** in this task: no new trust mechanism, no entity resolution, no relation
keys or subject/predicate/object facts, no NER, no temporal knowledge graph, no timeline UI, no
change to default resume ranking. Deferred items live at the end of this document.

## Data model

On `memory_records` and `extraction_candidates`, nullable:

- `valid_from`, `valid_to` — timezone-aware ISO 8601, interval `[valid_from, valid_to)`;
- `valid_from_precision`, `valid_to_precision` — `instant | day | month | year | unknown`
  (an interval is a shape, not a precision; each bound carries its own precision);
- `temporal_expression` — the exact evidence text the interval was derived from.

An absent bound is unknown/open — never an assertion of infinity.

`observed_at` is **derived, not stored**: the `created_at` of the source event containing the
temporal evidence span; manual derives without an evidence span use the admission time of the
first cited source event (amended during Phase A review: the cited observation, not the derive
command, is what "observed" means for a manual record). Multi-source memories resolve
`observed_at` through the evidence span's event, not through any aggregate.

Transaction time remains `created_at` (wall clock) with `seq` as the canonical total order.
Neither is overloaded to carry valid time.

## Non-negotiable invariants

1. **Two independent axes.** Transaction time (`created_at`/`seq`) and valid time
   (`valid_from`/`valid_to`) are never conflated, and `since`/`until` keep their existing
   transaction-time meaning.
2. **Branch-local, clock-safe `known_at`.** `known_at=K` resolves within the requested branch
   lineage to the greatest visible `seq` with `created_at <= K`; ancestors are visible only
   through their fork cutoff; ties and non-monotonic clocks resolve deterministically by `seq`.
3. **Known-at-aware projection.** All derived temporal state — effective intervals, conflict
   marks, `validity_status` — is computed **relative to the `known_at` cutoff**. Projecting from
   full history and then filtering by `known_at` is forbidden: a retroactive correction admitted
   after K must not close, open or contradict any interval as seen at K. The projection code is
   identified by `temporal_projection_code_version` in hit metadata and follows snapshot replay
   versioning, because it affects prompt content and exposure audit.
4. **Validity status is explicit and complete.** Every temporal hit carries
   `validity_status` ∈ `current | current_open | expired | not_yet_valid | unknown`, defined
   exclusively as the fixed predicate compositions in "Temporal logic core":
   - `current` — `contains(interval, now)` is definitely `TRUE`;
   - `current_open` — start definitely past, end unknown: a labeled presumption of
     continuation, not proof;
   - `expired` / `not_yet_valid` — the respective definite `FALSE`-side results;
   - `unknown` — every remaining case, including `UNKNOWN` predicate results from
     precision envelopes.
   `current=true` returns `current` and `current_open` (the dominant real-world case) and never
   `unknown`; `include_unknown_validity=true` adds `unknown` as a separately partitioned set.
   Prompt rendering distinguishes all three trust levels of "now": proven, presumed, unknown.
5. **Append-only history.** Temporal fields are fixed at version append time. Corrections append
   a successor via `supersedes_id`; predecessor rows stay byte-identical; retroactive
   corrections remain visible under transaction-time queries; every derivation is reproducible
   from canonical events.
6. **Deterministic normalization with explicit timezone policy.** Relative expressions resolve
   in the source-event timezone when recorded, otherwise UTC; a day-precision value denotes the
   complete calendar day in the resolution timezone; residual ambiguity lowers precision or
   quarantines. The original expression is always retained. Normalization accepts exact
   timestamps, dates, month/year values, bounded/open intervals, and evidence-anchored relative
   expressions — nothing model-guessed.
7. **Temporal metadata never raises authority.** It inherits the underlying memory's authority;
   it cannot confirm a candidate, supersede or invalidate trusted memory, or bypass evidence
   zones, quarantine or the policy ledger. Extractor confidence remains routing input only.
8. **Candidate identity includes temporal fields.** Candidate uniqueness covers normalized
   bounds and both precisions. A collision differing only in temporal data is retained or
   explicitly rejected append-only with a `rejection_reason`; it is never silently lost.
9. **Conflicts are projections.** MVP conflict detection operates only on explicit
   `supersedes_id` lineage and exact normalized-content matches with overlapping effective
   intervals and incompatible values. It must not guess that arbitrary prose describes the same
   property. Conflict marks live in the projection (optionally surfaced as append-only
   findings); no status is ever written to a memory-history row.
10. **Resume default unchanged.** Resume packets remain transaction-time by default; valid-now
    resume is opt-in; changing the default requires a separate decision backed by metrics.

## Retrieval contract

Orthogonal, individually optional controls on service, CLI, MCP and HTTP retrieval:

- `valid_at=T` — interval contains T (under Invariant 3 when combined with `known_at`);
- `known_at=K` — replay knowledge as of K (Invariant 2);
- `current=true` — `current` + `current_open` per Invariant 4;
- `include_unknown_validity=true` — adds the partitioned unknown set;
- `history=true` — all temporal versions with lineage.

Hits expose stored bounds, both precisions, `temporal_expression`, derived `observed_at`,
`validity_status`, effective interval when projected, temporal provenance, and
`temporal_projection_code_version`. Capabilities report temporal support and projection version.
Normalization, quarantine and conflict findings go through the existing append-only audit
pipeline without exposing private source text.

## Delivery phases

### Phase A — deterministic bitemporal core (this major version)

Schema + migration, normalization + timezone policy, known-at-aware projection, retrieval
controls, snapshot/replay integration, manual temporal input via derive paths. No model
involvement anywhere.

### Phase B — evidence-bound temporal extraction (gated, may ship in a minor after A)

Extractor candidates carry normalized intervals + both precisions + exact temporal evidence.
Gated by a new policy-ledger flag (`automatic_temporal_extraction_enabled`, default false,
bootstrap/transition rules identical to `task.md`). Requires RU/EN corpora covering explicit
dates, relative dates, intervals, mixed-precision bounds, ambiguity, negation, quoted/fenced
evidence zones, retroactive correction — and its own evaluation gates before enablement.

### Deferred (explicitly out of scope)

Relation keys and structured subject/predicate/object facts; entity resolution; temporal
knowledge graph; valid-now resume as default; contradiction-resolution operators (TOKI-style);
cross-memory semantic identity of properties.

## Required tests

- **Temporal core truth tables**: every predicate over every combination of
  known/unknown bounds × precision envelopes × endpoint orderings, three-valued results
  asserted exactly; Kleene connective laws and predicate dualities as property tests; no
  temporal comparison outside `temporal.py` (enforced by a grep-style test over the source
  tree).
- Pre-migration fixture database: byte-compatible results for every legacy call shape.
- Interval shapes: instant/day/month/year bounds, open-ended both sides, mixed start/end
  precision (e.g. `month` start, `unknown` end).
- Timezone: recorded source timezone vs UTC fallback; day-boundary ambiguity lowers precision or
  quarantines.
- `[from, to)` boundary cases for `valid_at`.
- `known_at` at a branch fork cutoff; two events whose wall-clock order disagrees with `seq`.
- **Combined bitemporal query**: after a retroactive correction at K2, `valid_at=T` under
  `known_at=K1 < K2` returns the pre-correction answer and under `known_at=K2` the corrected
  one — including the predecessor's effective interval appearing open at K1 and closed at K2.
- `validity_status` partitioning: proven current vs `current_open` vs separately partitioned
  unknown; unknown never in the primary current set.
- Candidate temporal collisions retained or explicitly rejected, never lost.
- Untrusted temporal evidence cannot confirm, supersede or mutate trusted memory; evidence
  zones and quarantine apply to temporal expressions.
- Append-only correction and retroactive-validity sequences; deterministic full replay; snapshot
  hash verification; projection version visible in hits and exposure records.
- Resume output unchanged without the opt-in flag.

## Acceptance criteria

1. Existing databases migrate additively; existing callers keep transaction-time results,
   verified against a pre-migration fixture.
2. `valid_at`, `known_at` and their combination produce deterministic, branch-lineage-safe,
   independently testable results; derived temporal state respects the `known_at` cutoff.
3. `created_at`/`seq`, `observed_at` (derived), valid time and effective intervals are never
   conflated; `observed_at` has exactly one definition.
4. Unknown validity cannot be consumed as current truth through the primary result set;
   `current_open` is visibly a presumption, not proof.
5. Every normalized temporal value has exact provenance or is explicitly unknown/quarantined;
   temporal corrections are append-only and replay-deterministic.
6. Temporal projections are versioned and hash-verifiable end to end (snapshots, retrieval,
   exposure audit); all temporal comparisons are confined to the temporal core module and its
   truth tables are exhaustively tested.
7. Full existing suite passes; automatic extraction (both kinds) remains disabled by default;
   `requirements-traceability.md` maps Invariants 1–10 to implementation and tests.
8. Documentation describes the result as bitemporal memory with provenance — explicitly not a
   temporal knowledge graph — and `docs/architecture.md` gains the temporal section.
