# Task: automatic evidence-bound memory extraction

## Goal

Remove the requirement for the primary agent to add `Goal:`, `Decision:`, `Fact:`,
`Failure:` and `Lesson:` markers during normal work, while preserving Joiny-Mnemonic's
provenance, append-only auditability and trust boundaries.

Explicit markers remain supported as an override, correction and confirmation path. The
extractor is optional: disabling it or losing the local model must not affect canonical event
capture, prompt delivery or existing marker-based consolidation.

## Binding invariants

1. The canonical event is durably committed before extraction starts. Extractors read only
   canonical `event.content` after secret and `<private>` redaction, never raw hook payloads.
2. Canonical events are never evicted, skipped or made unavailable for later extraction because
   of queue pressure, model failure, low confidence or validation failure.
3. Every extracted candidate cites an exact span in its current canonical source event and keeps
   `source_event_ids`. The model does not provide offsets; deterministic code computes them.
4. No exact and unambiguous quote match means no auto memory. The attempted interpretation may
   remain quarantined or rejected for audit and evaluation.
5. Auto extraction cannot create protected blocks or otherwise promote content into protected
   state. Trust-boundary transitions are gated symmetrically in both directions.
6. Every durable state transition is append-only and has provenance. Current statuses are
   rebuildable projections, never sources of truth.
7. Canonical events and the interpretation ledger are required backup data. Semantic indexes,
   graphs, current-status projections and resume projections remain rebuildable views.
8. The system guarantees at-least-once model execution and exactly-once durable effects for a
   given event and extractor configuration; it does not claim exactly-once inference.

## Data classes

Document three architectural data classes in `docs/architecture.md`:

1. **Canonical events**: authoritative, immutable source context.
2. **Interpretation ledger**: durable, immutable records of concrete non-deterministic model
   executions, candidates, transitions and links. These records are auditable but are not
   authoritative facts and cannot be reconstructed exactly by rerunning a model.
3. **Derived views**: disposable projections fully rebuildable from the first two classes,
   including semantic indexes, graph projections, current candidate status and resume ranking.

Future cleanup code must not treat the interpretation ledger as a rebuildable cache.

## Extraction pipeline

For each eligible canonical user or assistant message event:

1. Commit the redacted canonical event through the existing ingestion path.
2. Schedule extraction by durable database state; an in-memory notification only wakes a worker.
3. Build input from the current canonical event and, optionally, a bounded number of preceding
   canonical events as read-only context. Raw hook payloads are forbidden for both current and
   contextual messages.
4. Start a fresh stateless extractor invocation. Context may resolve anaphora, but every evidence
   quote must occur in the current event.
5. Request strict structured output containing only:
   - candidate memory type;
   - normalized content;
   - exact evidence quote;
   - model confidence.
6. Parse and validate the output deterministically. Locate the quote in canonical content and
   calculate offsets in code. Reject ambiguous repeated matches unless deterministic surrounding
   context resolves one occurrence; never accept model-generated offsets.
7. Compute the evidence zone in the same parser pass as offsets and store one of
   `prose`, `inline_code`, `fenced_code` or `blockquote`.
8. Commit the successful attempt, candidates, initial transitions, memory links and derived memory
   records atomically.

Do not attempt heuristic detection of rhetorical quotations in prose. That residual risk is
handled by `origin=auto`, lower ranking and explicit confirmation.

### Evidence-zone policy

- `prose`: eligible for auto memory subject to type, confidence and trust policy.
- `inline_code`, `fenced_code`, `blockquote`: untrusted evidence zones. Candidates may be retained
  in quarantine but do not enter resume, precheck or protected state before confirmation.
- A decision expressed only inside an untrusted zone may therefore require an explicit marker or
  confirmation. This is an intentional precision/recall tradeoff.

## Extractor plugin

Implement extraction as a new optional plugin entry-point category following the existing plugin
pattern. The core must gain no mandatory ML/runtime dependency.

- Use a structured information-extraction model such as NuExtract for the first implementation.
- Keep the backend configurable and test it through a deterministic fake extractor.
- Do not make GLiNER or another NER model a mandatory prefilter: "contains memorable information"
  is not an entity-density problem. Add a prefilter only if evaluation proves it does not damage
  recall.
- Support a kill switch and bounded worker concurrency.
- Store a structured extractor configuration descriptor and its canonical hash.

`extractor_config_hash` must include at least:

- model identity and weights/version;
- inference parameters;
- prompt and output schema versions;
- parser implementation version;
- evidence-zone and validator/policy versions;
- context-selection and normalization versions.

Store both the hash and its structured components so historical runs remain explainable.

## Interpretation ledger

Use immutable tables with no update/delete path. Exact names may follow storage conventions, but
the data model must preserve these relationships.

### Extraction runs and attempts

`extraction_runs` identifies logical work:

```text
id, event_id, extractor_config_hash, created_at
UNIQUE(event_id, extractor_config_hash)
```

`extraction_attempts` records each actual execution:

```text
id, run_id, attempt_no, outcome, started_at, finished_at,
error_code, redacted_error, raw_response_ref
UNIQUE(run_id, attempt_no)
```

Attempt outcomes include `succeeded`, `retryable_failure` and `terminal_failure`. A rebuildable
projection may expose logical run status as `pending`, `running`, `done`, `retryable` or `failed`.
Expired worker leases must make interrupted work retryable without mutating old attempts.

### Candidates

`extraction_candidates` is immutable and records:

```text
id, run_id, attempt_id, memory_type, normalized_content,
evidence_quote, evidence_start, evidence_end, evidence_zone,
confidence, created_at
```

Use a stable uniqueness key within a run, including type, normalized content and evidence span.
One event may yield accepted, quarantined and rejected candidates simultaneously; quarantine is a
candidate status, not an event/run status.

### Candidate transitions

`candidate_transitions` is an append-only status journal:

```text
id, candidate_id, from_status, to_status, source_event_id,
actor, rule_id, origin_evidence_type, replacement_candidate_id,
replacement_memory_id, created_at
```

Statuses include `auto`, `quarantined`, `confirmed`, `rejected` and `superseded`. Record the initial
`null -> status` transition explicitly. Every transition requires `source_event_id`; automatic
transitions also record the validator/policy rule and extractor run. `superseded` must identify its
replacement.

Materialize `candidate_current_status` only as a rebuildable projection of transitions.

`actor` is an operational producer vocabulary (`extractor`, `request_reducer`,
`explicit_marker`, plus named integrity actors), not a closed public enum.

### Candidate-memory lineage

Do not update a candidate to attach a memory ID. Add an append-only link:

```text
candidate_memory_links(
    candidate_id, memory_id, relation, source_event_id, created_at
)
```

Relations implemented in v1 include `derived`, `confirmed_as` and `supports`. `superseded_by` is
reserved; current supersession is represented by transition-journal replacement IDs. Memory
metadata must mirror `extraction_run_id`, `candidate_id` and `extractor_config_hash`. The complete
audit path must be queryable without joining by normalized text:

```text
canonical event -> extraction run -> attempt -> candidate -> transition
                -> candidate-memory link -> memory -> prompt exposure
```

## Confidence

Model confidence controls status, not existence:

- keep confidence in candidate metadata;
- make the auto/quarantine threshold plugin-configurable;
- below-threshold candidates enter quarantine instead of being discarded;
- optionally use calibrated confidence only for ranking inside the auto group;
- do not treat raw model self-confidence as calibrated probability.

Quarantined candidates are inputs to the future labelled evaluation set. Calibrate thresholds on
the Russian-language dataset before relying on them.

## Durable scheduling, retries and reprocessing

The append-only canonical event journal is the source of extraction work. In-memory queues contain
only coalescible wake-up signals.

- Discover eligible work by global canonical event `seq`; do not use branch-local sequence as the
  work cursor.
- Derive pending state from `(event_id, extractor_config_hash)` and immutable attempts. A scalar
  watermark may optimize scans only when no pending/retryable work exists below it.
- Apply bounded concurrency and retry/backoff without dropping canonical work.
- After retry exhaustion, retain a visible terminal failure and allow explicit retry.
- A crash after inference may repeat inference. Atomic uniqueness and commit rules must prevent
  duplicate candidates or memory effects.
- `reprocess` creates a run under a new `extractor_config_hash`; it does not erase or reset an old
  run. A parser/policy/code change therefore changes the hash by construction.
- Provide CLI/API operations to inspect backlog, process backlog, retry failures and reprocess with
  a new extractor configuration.

Expose `pending_events`, `oldest_pending_age`, `failed_events`, `last_success_at` and retry counts in
status and telemetry. Queue overload may increase latency but must not create memory gaps.

## Trust and promotion policy

### Automatic records

- Every extracted memory has `origin=auto` and exact canonical provenance.
- Auto records never create protected blocks.
- Resume includes eligible prose auto records marked `[auto]` and ranked below confirmed records.
- Quarantined records do not enter resume.
- Auto `failure` and `lesson` records do not influence precheck until confirmed.
- Prompt exposure telemetry retains candidate/run lineage for auto records.

### Explicit markers and commands

For a trusted user marker, try normalized `memory_type + content` matching against existing auto
records across source events. On a match, append a confirmation transition and link; otherwise
create a new explicit record. Also expose deterministic request operations by candidate/memory ID.

CLI, MCP, HTTP, Python API and assistant/tool content are not proof of human intent. From an
untrusted origin they may create only:

- `confirmation_requested`;
- `rejection_requested`;
- `supersession_requested`;
- `acknowledgement_requested`.

Every such operation must first append a canonical control event. Reducers create candidate,
policy or finding transitions from that event; commands and tools must not mutate a status
projection directly.

Any transition crossing the trust boundary in either direction requires sufficient origin.
Confirmed records cannot be rejected, superseded, demoted or removed from precheck solely by an
assistant, autonomous tool call or shell command.

### Cross-event deduplication

- Preserve per-run idempotency with stable candidate keys and transactional uniqueness.
- Search existing auto candidates by normalized `memory_type + content` across source events.
- An equivalent observation creates an append-only `supports` link to the existing memory rather
  than another independent resume item.
- A newer candidate that explicitly replaces an older value creates a proposed supersession link;
  it does not silently overwrite or demote confirmed memory.
- Conflicting candidates remain separately auditable and enter quarantine unless policy can resolve
  them without crossing the trust boundary.
- A trusted explicit marker upgrades a normalized match; if no safe match exists, it creates a new
  explicit memory instead of applying fuzzy destructive updates.

### Origin evidence

Keep authority and origin evidence as separate dimensions. At minimum distinguish:

```text
authority_level: auto | confirmed
origin_evidence_type:
  extractor | bootstrap_tofu | host_logical_user | signed_host_receipt | external_trusted_ui
```

`host_logical_user` means only that an event passed the configured host role/session checks. It is
not cryptographic proof that a human authored the event. Signed host receipts and an external
trusted UI are future extension points; none of the currently supported hosts provides such a
receipt.

SessionStart binding, `hook_runtime_verified`, parent-process checks and host-specific environment
markers may be used as hardening and diagnostics, but must not be documented as security
boundaries.

## Policy lifecycle and bootstrap

Workspace policy files are untrusted because an agent commonly has write access to the project.

- Reading a changed workspace policy may create only `policy_change_requested`.
- Active policy is represented in the durable ledger with its hash, version and activation event;
  it is not silently replaced by rereading the workspace file.
- `automatic_extraction_enabled` in active policy is the only runtime extraction switch. Installer
  configuration records backend/intent only; environment, workspace, CLI, HTTP and MCP inputs
  cannot bypass bootstrap or a trusted policy transition.
- Activation, rollback or replacement of policy follows the same trust-transition rules as memory
  confirmation.
- On an empty ledger, the first successful `joiny-mnemonic init` atomically emits
  `policy_bootstrapped` and records initial policy, hash, code version, project identity and
  `origin_evidence_type=bootstrap_tofu`.
- TOFU does not claim that a human ran `init`. Repeated bootstrap of the same known project is a
  security finding, not a silent first initialization.

## External witness registry

Add a user-level registry outside the workspace, for example under `~/.joiny-mnemonic/`. It is an
independent local witness, not a trust anchor: a process with the user's shell privileges may alter
both the database and registry.

Distinguish:

- `project_instance_id`: random identity of one initialized store;
- `repository_identity`: repository remote and optional initial commit;
- `canonical_path`: matching hint only, because projects move and repositories have multiple
  clones;
- `chain_id`: identity of the global event hash chain.

The Joiny-Mnemonic event chain is global across branches. Store one witnessed checkpoint per chain:

```text
project_instance_id, chain_id, head_seq, head_hash,
bootstrap_hash, first_seen_at, last_seen_at
```

Compare the current global chain with the last checkpoint:

```text
current_seq < witnessed_seq
    -> history_rollback

current_seq >= witnessed_seq and hash_at(witnessed_seq) != witnessed_hash
    -> history_divergence

current_seq >= witnessed_seq and hash_at(witnessed_seq) == witnessed_hash
    -> valid_extension and witness update
```

Update the witness best-effort after successful canonical commits and at store lifecycle/status
checkpoints. Database advancement with a stale witness is a valid extension; registry update and
database commit are not falsely presented as one cross-file atomic transaction.

Also detect:

- `known_project_database_missing`;
- `policy_rebootstrapped` when bootstrap identity/hash changed;
- `external_witness_missing` when the database survives but its witness does not;
- an undeclared chain replacement.

A legitimate restore/import may declare a new `chain_id`, but must preserve linkage to the old chain
and emit a visible finding/event; it is not silently equivalent to valid extension.

Document precisely: the registry detects loss or replacement only while the independent witness
survives. Non-repudiation requires an external or OS-protected anchor.

## Security findings and acknowledgement

Integrity/witness findings are append-only and sticky. Show active findings in `doctor`,
`memory_capabilities` and every resume package until acknowledged.

Acknowledgement is itself a provenance-bound transition:

- untrusted origins create only `acknowledgement_requested`;
- acknowledgement requires at least the same logical-user evidence used for other trust-boundary
  transitions;
- acknowledgement never deletes the finding or changes verification results;
- capabilities continues to report it as acknowledged;
- acknowledgement changes only repeated presentation policy;
- a later rollback/divergence creates a new sticky finding.

The meaning is "the user saw this specific incident", not "the incident no longer exists".

## Security model documentation

Update `docs/security.md` with the explicit boundary:

> Trust policy protects against escalation carried by interpreted content, including assistant
> text, tool output, quotations and extracted candidates. It does not protect against an agent
> with arbitrary shell execution as the user. Such an agent can emulate hooks, CLI, MCP and host
> metadata without writing directly to SQLite. Session binding, parent checks and environment
> markers add friction and diagnostics, not a security boundary. Strong separation requires
> distinct OS permissions, a trusted external service or signed host approval receipts.

Do not describe regex redaction as DLP, local hash chains as external integrity anchors, logical
user origin as verified human intent, or the local witness registry as non-repudiation.

## Retention and backup

- Keep interpretation-ledger records for at least as long as any linked memory or recorded prompt
  exposure exists.
- Never prune accepted, confirmed, superseded or exposed lineage independently of its memory.
- Unlinked, never-exposed rejected candidates may be archived under an explicit policy.
- Store raw model responses separately, redacted and compressed, with a documented retention
  policy; structured candidates and lineage outlive optional raw payloads.
- Backup/restore includes canonical events, interpretation ledger, policy ledger, findings and
  lineage. Derived projections may be rebuilt.
- Extend storage benchmarks to report amplification separately for canonical data, interpretation
  ledger, raw extractor payloads and rebuildable projections.

## Capabilities and user-facing status

Extend `memory_capabilities`/status with at least:

```text
extractor_available
extractor_enabled
extractor_name
extractor_config_hash
pending_events
oldest_pending_age
failed_events
last_success_at
quarantined_candidates
oldest_quarantined_age
active_security_findings
acknowledged_security_findings
witness_status
```

Resume must disclose stale auto-memory with `oldest_pending_age` rather than silently presenting an
apparently complete package.

## Evaluation

Create a versioned, manually labelled primarily Russian-language corpus covering:

- goals, decisions, facts, failures and lessons in ordinary prose;
- negative examples with no memorable information;
- anaphora requiring bounded prior context;
- repeated facts across events and sessions;
- fenced code, inline code and blockquotes;
- rhetorical quotations in prose;
- prompt injection and assistant text imitating markers/confirmation;
- tool output and other untrusted event kinds;
- secrets and `<private>` regions;
- low-confidence, malformed and ambiguous evidence;
- retries, crashes, reprocessing and model/parser upgrades.

Report precision, recall and F1 by memory type and evidence zone, plus:

- exact-evidence acceptance rate;
- false trusted/protected records (target: zero in adversarial fixtures);
- quarantine rate and confirmation yield;
- duplicate rate across events;
- extraction latency, backlog age and retry rate;
- storage amplification;
- resume inclusion and ranking correctness.

Do not enable automatic extraction by default until the labelled Russian corpus establishes an
explicit acceptable precision/recall baseline. Record the chosen thresholds and corpus version.

## Required implementation order

1. Add failing schema/provenance/trust tests and document the three data classes.
2. Implement immutable interpretation-ledger storage, transaction boundaries and projections.
3. Implement durable work discovery, attempts, retries, crash recovery and reprocessing with a
   deterministic fake extractor.
4. Implement evidence parsing, exact quote validation, zones, confidence quarantine and
   cross-event dedup/supersession behavior.
5. Add resume/precheck ranking and exposure lineage while preserving existing marker behavior.
6. Add request/transition APIs and symmetric trust gating, including policy bootstrap/lifecycle.
7. Add witness registry, rollback/divergence findings and provenance-bound acknowledgement.
8. Add the optional local extractor plugin and kill switch.
9. Add the Russian evaluation corpus, metrics and storage/performance gates.
10. Update architecture, security, integrations, evaluation, performance and backup documentation.
11. Run the full existing and new test suite, integrity verification, `git diff --check` and a
    clean-install/plugin-disabled compatibility check.

## Acceptance criteria

- Ordinary agent/user prose creates useful memory without primary-agent markers.
- Existing explicit-marker behavior and all current integrations remain backward compatible.
- Every auto record is traceable through exact canonical evidence, run, attempt, candidate,
  transition and memory link.
- Protected blocks and confirmed precheck state cannot be created, demoted or suppressed by auto
  extraction or untrusted request paths.
- Quarantined and failed work remains visible and retryable; no queue behavior loses canonical
  extraction work.
- Resume clearly separates confirmed, auto and quarantined/non-included state and reports extractor
  lag.
- Reprocessing with a changed model/parser/policy preserves old audit history and creates a new run.
- Rollback, divergence, missing database/witness and rebootstrap conditions produce durable visible
  findings when the local witness survives.
- Documentation states the shell-access threat boundary without claiming verified human origin or
  local non-repudiation.
- The feature remains optional and the core still works with no ML dependencies installed.
