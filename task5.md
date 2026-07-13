# Task 5: state maintenance and the Hindsight harvest

## Status

Proposed next milestone. Two motivations, one live-run finding each:

1. **State maintenance.** The trust model protects writes but nothing maintains truth: a
   completed task stayed in `open_tasks` (its completing Write event was in the same database,
   105 seconds after the task), both hosts then paraphrased the stale entry wrongly, and the
   protected block became the best-defended lie in the system. Manual `block-set` hygiene is an
   escape hatch, not a workflow.
2. **Retrieval quality.** Hindsight (vectorize-io, MIT, arXiv 2512.12818) reaches 91.4% on
   LongMemEval with a retrieval stack whose merge/rank/temporal layers are almost entirely
   deterministic. Everything below marked [HS] is adopted from their paper or source with
   constants they shipped; attribution stays in code comments. Their weaknesses confirm our
   differentiators: no bitemporal model (they credit Zep and stop), no provenance to source
   spans, no tamper evidence, no injection trust model, mandatory LLM+Postgres runtime.

Explicitly deferred, not in this task: `hindsight-bridge` plugin (their reflect layer over our
canon), LLM-driven observation consolidation (belongs to the gated Phase B extractor domain),
cross-encoder reranking (model dependency; their own passthrough mode — seed scores from RRF
rank — is exactly our zero-dep fallback).

## Part A — evidence-bound state maintenance

The system already captures the evidence that tasks complete; it just never looks. Closure
must be deterministic-first and provenance-bound, in the existing trust model.

### A1. Task-completion reconciler

- A deterministic pass (hook-time, same cadence as consolidation) compares live `open_tasks`
  entries and open `task`-type memories against canonical events admitted after them.
- Evidence classes, v1 (deterministic only):
  - file evidence: the entry references a path (normalized match on basename + parent) and a
    later captured tool event created/modified that path;
  - command evidence: the entry quotes a command and a later tool event ran it successfully.
- A match never silently mutates protected state. It appends a canonical
  `task_completion_detected` event citing both sides (task source event, completing event) and
  — under a new policy-ledger flag `automatic_task_closure_enabled` (bootstrap/transition rules
  identical to extraction):
  - flag on: writes the new `open_tasks` block version with the entry removed, source events =
    the completion evidence (closure is auditable: "closed by evt_...");
  - flag off (default): the detection surfaces as a finding in capabilities and as one line in
    the resume packet ("open task N appears completed by evt_...; confirm to close").
- The completed entry's record is superseded, never deleted; block history keeps every version.
  (Hindsight's move-don't-flag `invalidated_memory_units` table validates the principle — hot
  paths carry no state predicate — which block versioning already gives us.)

### A2. Block hygiene findings

Extend the staleness machinery (git-based today) to protected blocks: entries referencing
files that no longer exist, tasks older than a configurable age with no referencing events,
decisions whose content ends in `?`. Warning-only, surfaced through the existing findings
pipeline; ranking-neutral, like all staleness.

### A3. Marker ergonomics guard

Live finding: `DECISION: <вопрос>?` — a user querying with a marker wrote a question into a
protected block. Deterministic guard: a user marker whose content ends with `?` creates the
searchable record but routes the block write to `block_change_requested` (same *_requested
discipline as everything else) with a one-line notice in the next injection. No LLM, no
heuristics beyond the terminal question mark.

### A4. Citation over recall

Injection alone is proven insufficient: with the correct block in context, models paraphrased
it wrongly and attributed the fabrication to ACTIVE MEMORY when asked directly. Assimilation
is probabilistic; quoting must become cheaper than recalling:

- default installation registers MCP (drop the "MCP later" split): the agent gets
  `memory_blocks` (verbatim protected-state dump, trivial new tool), `memory_search`,
  `memory_source`;
- the packet's durable-capture instruction gains one sentence: questions about decisions,
  tasks or constraints should be answered by calling the memory tools and quoting, not from
  recalled context;
- the terminal trusted restatement (shipped) and this rule are mitigations, not guarantees —
  provenance remains the arbiter and the docs keep saying so.

## Part B — retrieval upgrades [HS]

All deterministic, all stdlib, all layered onto existing arms (FTS5, semantic plugin, graph
plugin) plus one new arm. Constants are Hindsight's shipped defaults; keep them until our own
benchmark (Part C) justifies tuning.

### B1. Temporal retrieval arm

Phase A stored valid-time intervals; nothing retrieves by them yet. New arm, active only when
the query parses to a date range:

- rule-based parser (~150 lines, regex + datetime): explicit dates, `Month YYYY`,
  yesterday/last week/month/year/weekday, and fuzzy windows — "a couple of days ago" →
  [now−3d, now−1d], "a few weeks ago" → [now−5w, now−2w], RU equivalents; single dates expand
  to the full day; resolved against `query_timestamp` (caller-supplied anchor, defaults now);
- hard interval-overlap filter: candidate's event interval — `[valid_from, valid_to)` when
  present, else the source event's admission time as a point — must intersect the query range
  (evaluated through the temporal core's envelopes; three-valued semantics apply: definite
  overlap ranks above possible);
- midpoint proximity score `1 − |mid_f − mid_Q| / (Δ/2)`;
- coverage selection: pool up to 60 in-window candidates, keep 10 spread across 8 time buckets
  so results span the window instead of clustering at its edge.

### B2. Reciprocal Rank Fusion

Replace the current score-mixing merge with RRF over all active arms:
`score(hit) = Σ_arms 1/(60 + rank_arm)`, absent → skipped. Per-arm ranks recorded in hit
metadata (`fusion_ranks`) — this feeds the existing `retrieval_search` exposure audit, so
"which arm surfaced this" becomes an auditable fact. Legacy behaviour: with a single active
arm, ordering must be unchanged (regression-tested).

### B3. Multiplicative secondary boosts

After fusion: `final = base × (1 + 0.2(recency−0.5)) × (1 + 0.2(temporal−0.5)) ×
(1 + 0.1(support−0.5))` where recency = `max(0.1, 1 − days/365)` over
`COALESCE(valid_from, observed_at, created_at)`, temporal = the B1 proximity (0.5 neutral for
non-temporal queries), support = normalized count of `supports` links plus confirmations on
the record. Multiplicative-around-1 keeps secondary signals proportional — they can nudge,
never flip, a strong lexical/semantic match. The existing ×0.85 non-confirmed damping is
unchanged and applied before boosts.

### B4. Index signal enrichment

- `text_signals` companion column in the FTS tables only: humanized dates from temporal fields
  ("June 5 2026"), file basenames, entity names when the graph plugin is present — BM25 starts
  matching "что было в июне" without polluting displayed content;
- the semantic plugin prepends `[Date: ...]` to text before embedding (their measured cheap
  win for temporal awareness).

### B5. Graph arm scoring (knowledge-graph plugin)

Where the plugin is installed, its arm scores expansion as
`tanh(0.5·shared_entities) + semantic_link_weight + (causal_weight + 1.0)`, per-entity fanout
cap 200. Plugin-side change; core only consumes the arm through B2.

## Part D — packet and reducer upgrades [HR]

Adopted from Headroom (headroomlabs-ai/headroom, Apache-2.0) — the strongest compression layer
in the field; items below are their deterministic mechanisms translated onto our canonical
model, attribution in code comments. Their ML compressor, proxy mode and output steering are
out of scope.

### D1. Cross-event verbatim folding in the transcript section

Coding agents re-display the same file bytes many times (cat → sed → diff → cat). The packet
assembler folds a later event's contiguous span that appeared VERBATIM in an earlier included
event into a pointer: `<folded: identical to evt_X> `. Their two hard invariants transfer
unchanged and match our model exactly: (1) prefix-monotonicity — later blocks only reference
strictly earlier ones by ABSOLUTE id (ours are event ids by construction), appending a turn
never rewrites an earlier one; (2) keep-earliest — the referenced original is always
physically in the packet; only large non-trivial spans fold. Pure stdlib, deterministic,
return-unchanged on any error.

### D2. Activity-based view maturation

Their measured insight: file-touch gaps are fat-tailed (next-touch p50 = 4 turns, p90 = 81),
so fixed hold windows fail. The assembler picks a tool-output representation by quiescence:
while the file is active (touched within `quiesce_turns`, default 5 interaction groups) the
raw/verbatim form is used; once quiet, the compact view with its source id. Deterministic —
activity is computable from `files` on captured events; `max_hold_groups` bounds the verbatim
period. Raw stays canonical as always; this only changes which representation the packet picks.

### D3. JSON-array reducer (SmartCrusher recipe)

New command-aware reducer for JSON-array tool outputs, their shipped recipe: keep first 30% +
last 15% + change-points + anomalies (variance/uniqueness thresholds 2.0/0.1), dedup identical
rows, cap 15 items; PREFER a lossless tabular re-render (CSV-schema/markdown-kv) when it saves
≥ 15% — lossless needs no retrieval round-trip, so it gets the lower bar; otherwise the lossy
path appends an in-band sentinel AT THE DROP SITE: `{"_dropped": "<view_id: N rows, expand via
memory_source>"}` — the retrieval affordance sits exactly where content is missing, not in a
footer. Existing no-expansion guard applies unchanged.

### D4. Protected patterns, fail closed

Generalize our critical-signal preservation (currently reducer-hardcoded) into configuration:
a list of regexes whose matching rows/lines MUST survive every reduction verbatim — not
dropped, not sentinel-replaced. If splice-back cannot guarantee it, the reducer fails closed
and returns the raw form. This is their `audit_safe` mode and it is philosophically ours:
compression may never eat the compliance row.

### D5. Over-compression feedback (their TOIN, de-LLM-ified)

They learn compression aggressiveness from retrieval-after-compression signals. We already
record both halves: `prompt_injection` exposures (which view, which level) and exact-source
promotions. A deterministic offline report correlates them: command classes whose compact
views get promoted to source within the same task N% of the time are flagged
"over-compressed; default to fuller view". Warning-only, feeds a per-class reducer-level knob;
no ML, evidence in the existing audit tables.

## Part C — the number

Adapt Hindsight's benchmark shape to the existing `evaluate-runner` protocol: a dataset
adapter (4 methods: load, item id, sessions-for-ingestion, qa-pairs), ingestion through
`append`+`consolidate`, answers through `search`+prompt assembly, an external LLM judge
returning `{correct, reasoning}` (their per-category judge prompts are printed verbatim in
arXiv 2512.12818 Appendix A.4). Target: a LongMemEval-S accuracy figure for Joiny-Mnemonic in
`benchmarks/results/`, whatever it turns out to be. Until this exists, every retrieval-quality
comparison in our README stays qualitative and must say so.

## Required tests

- Reconciler: file-evidence closure end-to-end (task → captured Write → detection event →
  flag-off finding / flag-on new block version with completion provenance); command evidence;
  no match → no event; repeated runs idempotent (receipts).
- Trust: closure flag follows policy-ledger bootstrap/transition rules; untrusted origins
  cannot enable it; `?`-marker routes to block_change_requested; the searchable record is
  still created.
- Temporal arm: parser table (explicit, relative, fuzzy, RU/EN, day expansion); definite vs
  possible overlap ordering; coverage buckets spread; arm inactive on non-temporal queries.
- RRF: multi-arm fusion ranks vs hand-computed values; single-arm ordering byte-identical to
  legacy; `fusion_ranks` present in exposure records.
- Boosts: monotonicity (boost never reorders a pair whose base scores differ by more than the
  maximal boost spread); neutral signals are no-ops.
- Hygiene findings: missing-file task, aged task, `?`-decision each produce exactly one sticky
  finding through the existing pipeline.
- MCP default install: capabilities show the tools; `memory_blocks` returns verbatim block
  content.
- Folding: appending a new event never changes the rendered form of earlier packet blocks
  (prefix-monotonicity property test); folded span's referenced event is always included;
  small/trivial spans never fold.
- Maturation: active file renders verbatim, quiet file renders compact, a new touch resets the
  clock; `max_hold_groups` enforced.
- JSON reducer: lossless path wins at the 15% gate; sentinel carries recoverable id and count;
  protected-pattern rows survive both paths or the reducer returns raw (fail-closed test).

## Acceptance criteria

1. A completed file-task is detected without human action; with the policy flag on it closes
   itself with provenance to the completing event; with the flag off the user sees one clear
   finding. Nothing is ever deleted.
2. No new trust transitions: closure, guards and hygiene all ride existing mechanisms
   (policy ledger, *_requested, findings, block versions).
3. Temporal queries ("что решили в июне", "a few weeks ago") retrieve interval-matching
   records ranked by proximity, with coverage across the window.
4. Fusion and boosts are exactly reproducible from `fusion_ranks` + stored signals recorded in
   the exposure audit.
5. A LongMemEval-S number exists in benchmarks/results with the harness committed.
6. Full suite green; `requirements-traceability.md` maps every A/B item to implementation and
   test; Hindsight-derived constants carry source attribution comments.

## Post-implementation review backlog

Parts A/B/D shipped and were adversarially reviewed; HIGH findings (untrusted/failed evidence
closing tasks, path-boundary collisions, anchorless scans, parser crashes on digit runs,
Russian month false positives) plus M1/M4/M7/M8/M9/M10/L4/L6 are fixed with regression tests.
Deferred, in priority order:

1. Reconciler and temporal-arm cost on every hook delivery (M5/M6): persist a per-branch
   high-water seq, push kind/tool filters into SQL, cache anchors; cap temporal-arm scans.
2. `known_at`+fusion interplay (M3): rebuilt as-of ancestors re-enter with legacy-scale scores
   and lose fusion metadata — carry the fused score across the rebuild.
3. Temporal arm must honor `since`/`until` for the memory leg too (M2).
4. Spec deviations (M11): pending completions as a resume-packet line; hygiene as sticky
   findings with an ack path; configurable age threshold; `query_timestamp` caller anchor for
   day-boundary correctness in non-UTC sessions; `setup --yes` MCP default (interactive path
   done).
5. D3 anomaly retention (variance/uniqueness) explicitly de-scoped in v1; the traceability row
   covers dedup/quotas/change-points only.
6. Low-severity polish (L1-L3, L5, L7): word-boundary command matching, closure rewrite
   preserving untouched entry formatting, `_PATH` false positives in hygiene, cross-bucket
   definite-before-possible ordering, skip future windows for event evidence.
