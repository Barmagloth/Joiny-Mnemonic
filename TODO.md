# TODO — validation and usability

## Readiness statement (2026-07-17, user assessment — the honest one)

**The central user scenario is NOT proven: ordinary dialogue reaches a
decision, the agent emits a strict post-factum finalization tag, and only that
finalized outcome becomes trusted long-term memory.**
What demonstrably works: the append-only journal, hooks capture, resume
and MCP, lexical/semantic retrieval + reranker, provenance, snapshots,
settlement, explicit markers with manual confirmation, performance and
channel diagnostics. All of it is infrastructure AROUND the unproven
core loop. The shipped extraction backend (nuextract-local) was never
measured; a bridge-extractor research probe was mistakenly announced as
"gate passed"; the central acceptance was skipped and project readiness
was overstated. More importantly, automatic semantic extraction is the wrong
authority boundary for finalized decisions: it may discover something worth
asking about, but it must never turn proposals, questions, reasoning, or a bare
yes/no into trusted memory. Only a strict final tag emitted after the outcome
is known may authorize a durable record.

The core works; the project is unvalidated and the UX has sharp edges. This
file tracks the short list of work that actually changes that assessment —
benchmarks people can check, settlement without manual magic, repeatable
host-level proof. Ordered by impact. Statuses updated as items land; detailed
specs live in the task files, not here.

## 0. Post-factum finalization + dogfood -- P0

This supersedes automatic extraction as the central product path. The user is
not expected to write markers. The host agent must emit final tags after a
decision is actually resolved, for example:

    [DECISION] CONFIRMED: Use YAML for GPTShared configuration (proposal 42).
    [DECISION] DEFERRED: Revisit proposals 32, 33, 34 and 40 later.
    [DECISION] REJECTED: Do not adopt proposal 41.

Before those lines exist, the proposals are conversation only. They may remain
in the immutable transcript for audit, but they are ineligible for automatic
memory retrieval, resume, protected blocks, candidates, or ranking. Storing a
turn is not authority to recall it as memory.

- [x] Specify the exact standalone final-tag grammar and statuses. Final tags
      must carry enough content to stand alone; proposal ids are audit links,
      not the only meaning of a record
- [x] Accept final tags only from the installed host's assistant-finalization
      event; quoted, fenced, tool-output, retrieved, historical, and user-
      supplied lookalikes remain data and cannot finalize anything
- [x] Deterministically materialize only tagged outcomes. Missing, malformed,
      duplicated, stale-task, or contradictory finalization fails closed into
      quarantine; no semantic recovery or punctuation guessing
- [x] Hard-filter unfinalized assistant proposals/questions/reasoning from
      automatic resume and memory retrieval. Raw transcript remains available
      only through explicit source/context inspection and is labelled as data
- [x] Teach Claude Code and Codex, through native project instructions, to emit
      final tags after resolved choices and to ask whether a newly invented
      outcome should be recorded. No answer means no tag
- [x] Add hostile E2E fixtures: lost question mark, unanswered proposal,
      yes/no inversion, rejected/deferred alternatives, quoted/fenced fake
      tags, prompt injection asking for a tag, stale proposal id, and multiple
      simultaneous branches
- [ ] Bind benchmark/task acceptance to an immutable target identity. A report
      cannot settle a gate unless component, model revision, prompt/config hash,
      and acceptance contract match; a changed goal is a separate append-only
      transition
- [ ] Dogfood this repository with hooks + MCP + semantic-local + reranker-local,
      automatic extraction OFF. Verify Claude and Codex across fresh session,
      compaction, source lookup, confirmed/rejected/deferred outcomes, and undo

Done when: a normal yes/no decision creates exactly the post-factum tagged
outcome; an unanswered or merely proposed alternative is absent from every
automatic memory surface; both hosts recover the selected decision after a
fresh session without the user writing a marker.

## 1. Real LongMemEval-S run — IN PROGRESS

Baseline v1 running now (500 questions, local `claude -p` runner, Sonnet).
Interim signal: single-session-user strong, multi-session and preference weak
— context budget 4096 tokens / retrieval limit 24 look undersized for
multi-session aggregation, and the answer-only-from-packet prompt pushes
preference questions into abstention.

- [x] Harness (task5 Part C), local runner bridge, resumable runs
- [x] Signed reports (provenance + artifact hashes)
- [x] Baseline v1 checked in (2026-07-14, signed): 57.6% overall —
      single-session-user 84.3%, assistant 82.1%, knowledge-update 79.5%,
      temporal-reasoning 59.4%, multi-session 26.3%, preference 23.3%,
      abstention 28/30
- [x] Error analysis done (2026-07-14 probe series, 20 multi-session
      questions): pool holds 100% of gold sessions at limit 128 — retrieval
      exonerated; rank/12k 6/20, cap3 5/20, breadth 0/11 (killed), rank/20k
      5/20 — multi-session plateaus at ~25-30% regardless of packing and
      budget; the wall is turn-sized fragments (400-1200 tok) vs one-shot
      aggregation
- [x] Final run (2026-07-15, signed, product plugin path — semantic-local +
      reranker-local, 12288/64/rank): **88.0% overall** (440/500).
      Per type: ssu 98.6 / ku 96.2 / ssa 92.9 / ms 85.7 / temporal 84.2 /
      preference 60.0; abstention 28/30; retrieval recall ~100%; token
      saving 92.9%. Multi-session plateau broken by full-pool cross-encoder
      reranking in the engine (26.3% -> 85.7%)
- [x] README section with our numbers — ours only, no other systems' scores
- [ ] Remaining error-analysis target: preference (60%) — cross-encoder
      optimizes question-relevance while preference answers need breadth of
      taste evidence; and the temporal tail (84.2%)
- [ ] Option matrix (2026-07-16): one table of accuracy × cost per
      retrieval option set, stratified-60 frozen protocol, paired against
      the raw-run rows: lexical-only / +semantic / +reranker / +both
      (published config = +both). Each cell also cites its hook-timing
      mode (core_only vs installed_plugins already measured per scale).
      Measured on the live install 2026-07-16: plugins cost ~+70ms per
      fresh-process delivery (205ms -> 275ms on GPTShared), not the ~640ms
      the cold probe suggested
- [x] A/B with LLM extraction done (2026-07-16, staged paired probes,
      signed: benchmarks/results/distill-ab.md). Verdict per the decision
      rule: INSIDE the band (expected full-500 ≈ 87.4) with a typed
      redistribution — preference 60.0→66.7 (+3/−1 paired on all 30),
      knowledge-update 96.2→89.7 (0/−5 paired on all 78, every regression
      a stale-fact atom outvoting the later update). Flat distillation
      without supersession poisons knowledge updates → shape stays opt-in;
      item 6 inherits the sharpened target: update-aware distillation
      (supersede or validity-bound contradicted facts), not just extractor
      precision/recall
- [x] Update-aware cell measured (2026-07-16, --ingest distill-aware,
      deterministic token-containment supersession, signed:
      distill-aware-knowledge-update/): fixes the pure value-update class
      (+2/78 KU) at comparable collateral — 170 supersessions vs ~2 true
      updates in vivo (recurring topics are indistinguishable from updates
      by tokens); net −1, within the ±2.5pp run noise. Census of the 5
      flat-distill KU losses: 1 value-update, 1 low-overlap update, 1
      distiller-recall miss, 1 needs enumeration, 1 poisoned abstention —
      deterministic ingest-side supersession is ruled out as the fix.
      Next candidate shapes (rising cost): answer-time recency discipline
      over near-dup facts, write-time LLM reconciliation, entity-slot
      keying (distill-ab.md stage 2)
- [x] Cross-family re-judge done (2026-07-14, GPT-5.4 over all 500
      persisted answers, byte-pinned rows and dataset, verified by recount
      from raw batches, report signed): **87.6%** vs 88.0%, agreement
      98.8%, 6 flips (4 down, 2 up), zero flips in multi-session. The
      same-stack-judge caveat is now empirically bounded at ~0.4pp
- [x] Opus re-judge done (2026-07-15, signed): 89.0%, 9 flips (7 of them
      preference). Triangulation Sonnet/GPT-5.4/Opus = 88.0/87.6/89.0 —
      ±0.7pp; multi-session identical under all three judges; preference
      is the judge-sensitive type (60.0–76.7%)
- [x] Flip-set audit done (2026-07-15, benchmarks/results/flip-audit.md):
      12 unique flips; against the 3-judge majority the published number is
      slightly conservative (majority consensus 88.2%); one genuine
      leniency instance found and documented; wrong-lead-right-string
      archetype exists, adjudicated split; preference carries 7/12 flips;
      zero multi-session flips under any judge
- [x] Prompt ablation done (2026-07-15): plain prompt 70.0% vs tuned 86.7%
      on the same stratified 60 under the identical product stack — the
      benchmark-tuned prompt contributes ~17pp; README states it and how to
      reproduce (LME_PLAIN_PROMPT=1)
- [x] Variance repeats done (2026-07-15): frozen config, same stratified
      60, three points — 86.7 / 91.7 / 90.0 (published subset is the
      lowest); 5pp band on n=60 ≈ ±1.5-2pp at n=500; preference confirmed
      as the volatile type. Methodology hardening: COMPLETE — all seven
      weaknesses measured or closed

Done when: (achieved for the headline; error-analysis items continue)

## 2. Hook-path timing report — task6A

We shipped hot-path fixes (M5/M6) without measurements; that is debt.

- [x] Timing benchmark shipped and hardened to the strict acceptance
      (schema v2): per-stage breakdown, fixture sizes, cold/warm, two
      store scales, p50/p95/p99. Key numbers 2026-07-15: capture ~22ms;
      resume ~360-390ms of which packet assembly ~354ms (92% — the named
      next optimization target); reconciler 3-8ms; latencies flat from
      243 to 1411 events; cold service open 190ms core / 630ms plugins
- [x] p95 + cold budgets asserted as gates (`--assert-gates`); standing
      rule recorded: no new always-on feature before its hot path is
      observable in this report
- [x] Cold-feature invariant test: capture-path hook delivery imports no
      torch/sentence-transformers even with plugins installed
- [x] Stamped report in `benchmarks/results/hook-timing-latest.json`

Done when: a regression in hook latency fails `--assert-gates`. ACHIEVED.

## 3. Autonomous state maintenance with auditable undo — task6B/6C

The user must not police memory. Detection is already automatic; the default
must be automatic *closure* on strong deterministic evidence, with a passive
one-line notice in the next injection and a one-command revert (block history
makes undo lossless — that is what licenses the automation). Manual
settlement verbs exist for the ambiguous tail and for reversal only; a
growing pending queue is a detection-quality bug, not a UX feature. Live
fixture: the delme2.md completion detected on GPTShared (2026-07-14) should
have closed itself.

- [x] `candidate_kind` migration (schema v9); `task_closure` + `block_change`
      kinds (2026-07-15)
- [x] Evidence-strength ladder; strong evidence auto-applies by default
      (actor `system`, full audit trail); medium stays behind the legacy flag
- [x] Bidirectional reconciliation: re-added marker contests a closed entry;
      invalidated evidence auto-reverts the closure in `reconcile()` —
      wrong closures are caught by the system, not by user vigilance
- [x] Human-visible notice at action time: hook `systemMessage` on
      claude-code with the ready undo command + `AUTO-CLOSED RECENTLY`
      delta in the resume digest (24h, cap 3)
- [x] Consume-once settlement transitions, fail-closed policy, first-class
      `undo`; a reverted/contested closure never re-applies from the same
      evidence (`tests/test_settlement.py`)
- [x] 6C: `candidates show/settle` + MCP `memory_candidates` /
      `memory_settle_candidate` with trusted-origin checks for manual actors
      (`tests/test_settlement_surfaces.py`)
- [ ] Acceptance on a live host: a fresh GPTShared-style scenario closes the
      task with zero user actions; re-adding the marker contests it with
      zero user actions; `undo` restores the entry losslessly (unit-level
      already covered; needs one live hook delivery after venv update)

Done when: the common case needs no user action at all, and the wrong-closure
case costs one command.

## 4. Host-level E2E: Claude + Codex, repeatable

One-off passes rot. This should be a checklist (or script) run per release.

- [x] Claude Code pass (2026-07-14): store opens on new code, FTS signal
      rebuild transparent, hooks fire, injection delivered, reconciler
      detects the historical completion, temporal fusion live in search,
      reduction-report clean
- [ ] Codex pass: same checklist (hooks, injection, recall, PostCompact
      silence)
- [ ] Checklist written down (docs/host-e2e.md) so any release can be
      re-verified in ~15 minutes
- [x] Verb-flip RE-ATTRIBUTED (2026-07-15): a real transport bug was found —
      hook stdout used the console codepage while the host reads UTF-8, so
      injected Cyrillic could arrive garbled; after the UTF-8 fix plus the
      neutral packet wording, the nested probe quotes «создать» exactly.
      Confabulation claim downgraded to "likely encoding corruption"
      (two variables changed together; recurrence would reopen). Autonomous
      closure (item 3) remains the durable fix for stale entries as such

Done when: both hosts pass the same written checklist on the current release.

## 5. UX golden path — no manual magic

"Installed, asked a question, quoted the source, confirmed the closure"
should work without SQL, without hand-editing blocks, without reading the
architecture docs.

- [x] MCP registered by default in every setup path (task5 A4 + M11)
- [x] Pending completions surface in resume and capabilities
- [ ] Autonomous closure (item 3) removes the last manual step
- [ ] Fresh-user walkthrough: clean project → setup → work session → marker →
      recall question → `memory_source` citation → task completes → closure
      happens by itself, notice visible, nothing to confirm. Every step
      through shipped surfaces only. Timing note (2026-07-15): run this
      after the resume-packet surface stabilizes — task6C just changed the
      maintenance lines (bounded candidate index), measuring the walkthrough
      against a moving packet wastes the friction log
- [ ] Friction log from that walkthrough becomes the next UX batch

Done when: the walkthrough succeeds on a machine that never saw the repo,
performed by someone who did not build the system — and the memory required
zero maintenance actions from them.

## 6. Optional discovery extraction quality -- after P0 dogfood

LongMemEval (item 1) measures end-to-end recall. An extractor is now an
optional discovery assistant: it may notice a possible durable outcome and ask
whether to record it, but it cannot create trusted memory or promote a proposal
regardless of confidence. Its gate measures suggestion usefulness and nuisance
rate, while the P0 final-tag path owns authority.

Ordering (2026-07-15): sequenced AFTER the distill A/B in item 1 — the A/B
answers whether the distilled-facts shape pays at all (with a strong LLM
distiller as the ceiling); if it does, this corpus gets a concrete target
("close the gap to the A/B distiller"), if it does not, component tuning
is premature.

- [x] Fixture corpus + expected-candidate labels (2026-07-17: v2 corpora,
      70 en + 70 ru, 50 prose preference positives per language, 10 hard
      negatives + 3 untrusted-zone traps each; structurally validated by
      benchmarks/validate_corpus.py against the harness evidence rules)
- [x] Extraction eval runner + signed report (benchmarks/extraction_gate.py:
      claude-CLI bridge extractor as the system under test, memoized dual
      scoring — type-span for typing quality, exact-triple for provenance
      calligraphy — per-example audit JSONL, stamped combined report)
- [x] Recall/precision figures in the README next to the LongMemEval number
- [x] First corpus cycle complete (2026-07-17) — reframed after review
      (2026-07-17): this was a RESEARCH PROBE of the claude-code bridge
      extractor (Haiku), NOT the product gate. The shipped backend
      (nuextract-local) was never measured; 'gate passed' in earlier
      wording overstated the experiment's identity. Narrow claims only
      (review 2026-07-17): PROVEN — the gate mechanism stops a bad
      version (run 1 honestly failed: preference en 1.00/0.78,
      ru 0.97/0.67); type-span scoring works as a regression tool;
      the three declared injection zones held (all trap candidates
      quarantined, 0 auto-trusted out of 6 trap examples — that is the
      denominator, not 140). NOT PROVEN — generalization (prompt
      bridge-v2 was fixed on this corpus's own misses and re-judged on
      it: the corpus is now a development set; its post-iteration
      numbers en 0.98/1.00, ru 0.96/1.00 are dev-set numbers);
      non-preference types (n=7/language); broad injection robustness;
      run-to-run and model-version stability
- [x] Independent re-judge done (2026-07-17, local GPT-5.4 via codex,
      read-only sandbox): verdict `pass_with_narrowed_claim`, claim-by-
      claim with evidence pointers —
      benchmarks/results/extraction-codex-audit-20260717/. Confirms the
      preference dev-set numbers and the 0-of-6-traps framing; adds two
      concrete counterexamples to any cross-type claim: English fact
      precision 0.25 (1 TP / 3 FP), and exact-triple scores near zero
      (the headline measures type + span overlap only). Notable: part of
      the fact FPs sit on lines that DO carry a fact the gold did not
      list (pref-005 vegetarian, pref-030 cat names) — single-type gold
      is itself a corpus defect to fix in the held-out tranche
      (multi-type golds)
- [x] Backend smoke superseded (2026-07-26): nuextract-local does not work
      with Russian text and is excluded from stage 6 candidates. New local
      candidates: qwen3-4b, gemma-3-4b, gliner-multilingual (ROADMAP §9)
- [ ] Model-agnostic local extractor connector: one generic backend where the
      user swaps the model via config only (model id, revision, inference
      params in a single config; OpenAI-compatible / llama.cpp endpoint; no
      new Python package per model). Swapping must change
      ExtractorConfig.canonical_hash so signed eval reports never carry over.
      Document the swap as one short recipe and verify it on at least two
      different models. Required before the stage 6 evaluation runs.
      LANDED 2026-07-27: core contract (`extractor_backend.py`: prompt, schema
      and backend descriptor, all hashed), `ExtractorConfig.for_backend`,
      config-file backend block, plugin `local-llm` (openai_compatible +
      llama_cpp over loopback), recipe in `docs/extractor-connector.md`,
      14 checks in `tests/test_extractor_connector.py`. REMAINING: run the
      recipe against two real model runtimes — that lands with the candidate
      smoke below, not against a stub server
- [ ] Connector contract must admit a REMOTE LLM backend later (design
      constraint now, implementation deferred): a remote API endpoint or a
      host-CLI bridge (e.g. `claude -p` / `codex exec`) is just another
      backend selected by config, behind the same Extractor contract. The
      config schema must not assume the model runs locally: endpoint URL,
      transport kind (local runtime / remote API / CLI bridge) and model
      identity all belong to the hashed ExtractorConfig, so a remote swap
      invalidates signed local eval reports exactly like a local swap
      (JM-INV-007). Remote backends stay candidate-only like every
      extractor — no agent_finalized, no trusted memory. Stage 6
      evaluation itself runs on the local candidates only
- [ ] Widen the stage 6 field (requested 2026-07-27): measure `qwen3-8b`
      alongside the two 4B models, and add Haiku and Sonnet run through the
      host CLI as a minimal `cli_bridge` transport. This promotes the remote
      contract above from deferred to partly implemented: the CLI bridge is
      the first non-local transport, so it must carry its own transport kind
      and model identity into the hashed `ExtractorConfig`, and a CLI model
      stays candidate-only like every other extractor. Prerequisites:
      `qwen3-8b` needs a catalogue entry (weight URL + GGUF sha256, ~5GB at
      Q4_K_M); the CLI bridge needs schema-constrained output, since `claude
      -p` has no `response_format` — a schema violation must fail loudly, not
      degrade into prose. Note the corpora leave the machine for CLI models,
      unlike every measurement so far
- [x] Executable JM-INV-007 gate (2026-07-27): `extractor_evaluation_target.py`
      freezes the target BEFORE a run — corpus bytes, extractor name/version,
      hashed ExtractorConfig (model, revision, inference, prompt hash, schema
      hash), the scored memory types, the thresholds and the checker version —
      and `decide()` is the only place `passed` is computed: any field
      differing from the frozen target refuses the run outright, and a matching
      system still has to clear precision >= 0.90 and recall >= 0.70 per
      language, a cross-language precision gap <= 0.10 and zero false trusted
      records. Reports are append-only dated files (`latest.json` is a pointer,
      never a substitute); a re-run producing different content for an existing
      dated name raises `report_would_be_rewritten`. Runner:
      `benchmarks/stage6_extractor_eval.py --freeze` then `--target`.
      Verified by 15 checks in `tests/test_stage6_extractor_gate.py`, including
      an end-to-end runner pass against a STUB extractor (wiring only — no
      model quality is claimed by it)
- [x] Model + runtime provisioning as part of installation (2026-07-27):
      `model_provisioning.py` (content-pinned catalogue — weight sha256 taken
      from the HF tree API `lfs.oid`, NOT the ETag, which is a Xet hash — plus
      a pinned llama.cpp build, atomic verified download, generated backend
      block whose `revision` carries the weight digest) and
      `managed_runtime.py` (lazy start of ONLY the provisioned endpoint, health
      reuse, failures surface as a wakeup error, never fatal). `setup
      --extractor-model <key>` downloads, installs `local-llm` and configures
      the backend; there is no separate model-installation step. win-x64 only
      for now (`platform_not_provisioned` elsewhere). 10 checks in
      `tests/test_model_provisioning.py`; verified live end to end: server
      started lazily, real schema-valid candidates returned
- [x] Flake fixed (2026-07-27):
      `test_telemetry.RetrievalTelemetryTest.test_hook_retry_deduplicates_prompt_exposure`
      asserts a retried hook returns an identical payload, but the packet
      embedded `oldest_pending_age=<n.n>s` recomputed per call, so two calls
      that straddled a rounding boundary differed (`0.1s` vs `0.2s`).
      Usage-sample dedup itself always held. The disclosure now renders at a
      resolution proportional to its size (`under a minute` / `Nm` / `Nh` /
      `Nd`): a staleness line says the backlog is old, and spending a tenth of
      a second of precision on a seven-day backlog was never information. The
      retry is byte-identical while the state it describes is unchanged
- [ ] Implicit extractor selection is now ambiguous: with no `extractor.name`
      in configuration `MemoryService` still picks the alphabetically first
      installed extractor plugin, and provisioning means `local-llm` is present
      almost everywhere. Extraction cannot run without an explicit policy
      transition and the connector refuses a missing backend, so nothing runs
      unasked today — but the selection should become explicit rather than
      alphabetical before extraction is ever enabled by default
- [x] Candidate smoke for qwen3-4b and gemma-3-4b (2026-07-27): both ran the
      full dev corpora (70 EN + 70 RU, `extraction_*_v2`, ONE run each — no
      stochasticity measured, NOT held-out) through the generic connector.
      JSON validity 1.0 everywhere, mean latency 1.3-2.3 s. Signed reports in
      `benchmarks/results/stage6/`. Untyped prompt (`connector-v1`): qwen
      recall 0.06 en / 0.08 ru because it labelled preferences `decision`
      (56/59 false `decision`), and produced one false-trusted candidate on an
      adversarial ru example; gemma 0.85/0.75 en, 0.89/0.46 ru. After adding
      memory_type definitions to the shared prompt (`connector-v2-typed`):
      qwen 0.852/0.963 en, 0.882/0.849 ru, gap 0.03, zero false-trusted;
      gemma 0.831/0.907 en, 0.756/0.630 ru. NEITHER passes the gate — both
      miss `min_precision` 0.90 — but qwen is now the leading candidate and
      the only remaining failing check for it is precision
- [x] Classify the qwen precision gap (2026-07-27): added
      `--dump-predictions` to the runner (diagnostic file beside the report,
      never inside it) and re-ran the frozen `connector-v2-typed` target. The
      debug run reproduced the published report byte for byte. ALL 15 false
      `preference` candidates are `trap-*` examples with an empty gold: other
      people's preferences, hypotheticals, questions, momentary wants,
      sarcasm, negation, fiction and explicitly ended past preferences. Zero
      span errors, zero type errors among them — the gap is entirely a
      durability/attribution discrimination failure
- [ ] The precision gap survived the first repair attempt. `connector-v3-scoped`
      added an explicit negative rule ("extract only what the user themselves
      holds durably right now… not someone else's, hypothetical, a question,
      momentary, sarcastic, negated or ended") and made both models WORSE:
      qwen 0.863/0.815 en and 0.829/0.642 ru, gemma 0.725/0.685 en and
      0.629/0.415 ru with one false-trusted candidate. 14 of the 15 traps
      survived, and the rule caused 27 new `preference`->`fact` relabels
      (9 en / 18 ru) that did not exist under v2. Prompt reverted to
      `connector-v2-typed`; the v3 reports stay published. Next attempts should
      be measured against something other than trap-only prose rules — e.g.
      an explicit "who holds it / is it still true" field in the schema, or
      raising `auto_threshold` so traps land quarantined rather than auto
- [x] Gate audit repairs (2026-07-27, `CHECKER_VERSION` -> `stage6-extractor
      -gate-v2`, so every v1 target must be re-frozen; no published report is
      affected, all six already read `passed: false`):
      (a) a missing language report was accepted, so a run that reported only
      the stronger language could pass a target naming both — now
      `language_coverage_mismatch`;
      (b) `git_dirty` was recorded by provenance but never read by the
      decision, so a dirty tree could produce `PASSED` — `decide` now takes a
      required `worktree_clean` and treats unknown as dirty;
      (c) `revision` truncated the weight sha256 to 16 chars — now the full
      digest;
      (d) a same-day repeat run of one configuration could not be published
      (`report_would_be_rewritten`), which blocked the stochasticity repeats
      the plan requires — the filename now carries the report's content hash;
      (e) the report records `prompt_text`, not only the prompt hash
- [x] Publish the evidence behind the published claims (2026-07-27):
      `benchmarks/results/stage6/diagnostics/` holds the prediction dumps,
      their classifications and the reverted `connector-v3-scoped` prompt,
      each pinned by sha256 in `manifest.json`; the classifier is a committed
      tool (`benchmarks/stage6_classify_false_positives.py`), and a test
      proves the archived prompt reproduces the `extractor_config_hash` of
      both v3 reports
- [ ] Re-freeze both targets under checker v2 before the next measurement, and
      run from a clean tree — the six published reports were all measured from
      a dirty tree (`git_dirty: true`), which under v2 alone would deny
      `PASSED` regardless of the metrics
- [x] Auto-trusted wrong candidates are measured now (2026-07-27):
      `false_trusted` only counts examples flagged `adversarial`, so the
      semantic traps landed with `initial_status: auto` while the metric read
      0. Rather than redefine a metric whose narrow question is still worth
      asking, the report carries a second one, `auto_trusted_false_records` —
      every wrong candidate that arrived already trusted, attack or not — and
      the gate holds it to the same threshold of zero
      (`max_auto_trusted_false`). On the published qwen v2 dump the pair reads
      0 and 27. This moved `CHECKER_VERSION` to `stage6-extractor-gate-v3`, so
      the pending re-freeze covers it too
- [ ] Close the precision gap with a second pass, not a better prompt
      (state of the art surveyed 2026-07-27). Three findings, all pointing the
      same way. First, our single-pass numbers are not the weak part: Mem0 —
      the highest-precision system in a 2026 comparison — reports precision
      0.446 on LOCOMO, where our qwen3-4b sits at 0.85/0.88 on our own dev
      corpora. Different benchmark and different denominator, so this is not
      a ranking; it does say that 0.90 from one prompted pass is not what the
      field achieves. Second, every training-free approach converges on a
      verifier pass that judges each candidate against its own evidence
      before promotion — which is exactly what `connector-v3-scoped` tried to
      do inside the extraction prompt and failed at. Third, corroboration:
      one mention should not promote, a repeated one should. Concretely for
      us: (a) a verifier stage over `(candidate, evidence_quote, surrounding
      turn)` answering "does the user themselves hold this, right now"; (b)
      raise `auto_threshold` so an unverified candidate lands quarantined
      instead of auto — the traps then cost recall-into-quarantine, not
      `auto_trusted_false`; (c) treat a second sighting as the promotion
      signal. Note (a) doubles extraction cost per event; measure it as its
      own configuration with its own frozen target, not as a prompt tweak
  - [x] (a) built 2026-07-27: `verify_candidates` on `ExtractorConfig`,
        `VERIFICATION_PROMPT`/`VERDICT_JSON_SCHEMA` in the core, `verify()`
        on `local-llm` over both transports, `--verify-candidates` on the
        runner. A rejection quarantines rather than drops. The flag is
        absent from the descriptor when false, so no published one-pass
        report was invalidated by the feature existing; turning it on does
        move `canonical_hash`, which is the case that should
  - [x] (a) the metric it was built to move now exists (2026-07-28). Audit
        finding: a rejection quarantines rather than drops, and the evaluator
        counted quarantined candidates in `predicted`, so the second pass
        could not move the precision the gate read. Split into
        `candidate_precision/recall` (every candidate — detection and review
        queue) and `trusted_precision/recall` (`initial_status: auto` only —
        the only family automatic enablement turns on), plus
        `quarantine_reasons` by `rule_id`. Gate v4: candidate 0.90/0.70,
        trusted 0.90/0.50, both trusted counters zero. The trusted recall
        floor stops a reject-everything verifier from scoring perfect
        precision on an empty set; pre-split reports are refused rather than
        reinterpreted
  - [ ] (a) is built but **unmeasured**: no two-pass target has been frozen
        and no two-pass report exists. Nothing may be claimed about its
        effect on precision until one is published
  - [ ] (b) raise `auto_threshold` — not started
  - [ ] (c) second sighting as the promotion signal — not started
- [ ] gliner-multilingual still needs its own span-extraction adapter: it does
      not speak the chat/JSON-schema protocol the connector uses, so it cannot
      be measured through `local-llm` at all
- [ ] PRODUCT DISCOVERY GATE proper: run held-out scoring against the exact
      shipped backend and frozen configuration. Passing permits suggestion-only
      deployment; it never grants authority to write finalized memory
- [ ] Held-out tranche authored AFTER prompt freeze + repeat runs for
      stochasticity — required before any enablement decision; held-out
      design notes: multi-type golds for multi-assertion lines, more
      fact/decision/task/failure/lesson positives before any cross-type
      claim

Done when: discovery extraction is measured on held-out data and can be enabled
or rejected without affecting the correctness of the strict finalization path.
