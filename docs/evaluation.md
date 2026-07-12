# Evaluation

There are two deliberately separate evaluation modes.

## 1. Evidence-presence diagnostic

`evaluate` checks whether literal evidence strings are present in each rendered context:

```json
[
  {
    "id": "release-resume",
    "query": "resume release plan",
    "required_evidence": ["Aurora", "SQLite", "Friday"],
    "branch_id": "main"
  }
]
```

```powershell
joiny-mnemonic evaluate evals/reference_resume_tasks.json --resume-budget 1500
```

The report declares:

```json
{"evaluation_mode":"evidence-presence-diagnostic","task_level":false}
```

This mode is useful for deterministic regression tests. It does not measure whether an LLM or
agent completed a task. There is no default 95% production claim. `--minimum` is optional and
only gates this diagnostic ratio.

## Exposure correlation boundary

Usage reports count `retrieval_search` and `prompt_injection` exposures and retain task/session
IDs so later completed/blocked task versions can be correlated with what was shown. This is
observational data only: exposure does not prove causal usefulness, does not reinforce a record,
and does not alter retrieval ranking.

## 2. External task runner

`evaluate-runner` sends the same task once with full-history context and once with the real
budgeted resume context. The external runner can call an LLM, run an agent, execute tests, or use
a domain-specific judge.

Task file:

```json
[
  {
    "id": "release-change",
    "query": "resume release plan",
    "task_input": "Apply the pending release change and run its tests.",
    "expected_output": "optional runner-specific oracle",
    "branch_id": "main",
    "metadata": {"test_command": "python -m unittest tests.test_release"}
  }
]
```

Invocation:

```powershell
joiny-mnemonic evaluate-runner tasks.json `
  --runner-command '["python","project_eval_runner.py"]' `
  --runner-timeout 600 `
  --resume-budget 1500 `
  --minimum 0.95
```

`--runner-command` is a JSON argv array, not a shell command string.

### Runner request

The runner reads one JSON object from stdin for every task/policy pair:

```json
{
  "task": {
    "id": "release-change",
    "query": "resume release plan",
    "required_evidence": [],
    "branch_id": "main",
    "task_input": "Apply the pending release change and run its tests.",
    "expected_output": "optional runner-specific oracle",
    "metadata": {"test_command": "python -m unittest tests.test_release"}
  },
  "context": "[MEMORY PACKET] ...",
  "context_tokens": 1432
}
```

It must write one JSON object to stdout:

```json
{
  "success": true,
  "score": 1.0,
  "output": "tests passed",
  "metadata": {"tests": 42}
}
```

`score` must be between 0 and 1. If omitted, it defaults to 1 for success and 0 for failure.
A non-zero process exit becomes a failed run with captured stderr.

### Report and gate

The task-level report declares `task_level: true` and contains:

- task success and score;
- output and runner metadata;
- rendered context token cost;
- render and task latency;
- `score_vs_full_history`;
- aggregate success rate and score per policy.

`--minimum 0.95` gates resume score against the full-history score. The gate rejects diagnostic
reports, so evidence recall cannot accidentally be presented as task-level quality.

## Explicit-promotion boundary

The evidence-presence diagnostic intentionally exposes the boundary between canonical retention and automatic resume. A fact mentioned only in ordinary dialogue can score `quality_vs_full_history = 0.0` after enough later history: full history still contains it, and event search can recover it, but no typed memory or protected block was created for the compact resume packet.

With the optional extractor disabled, Joiny-Mnemonic does not infer facts to close this gap. When an extractor is explicitly enabled, ordinary prose may create lower-ranked exact-evidence auto memory, while quarantine and backlog remain visible. Independently, each runtime resume packet gives the agent a protected `[DURABLE MEMORY CAPTURE]` instruction. Durable, evidence-backed information should be promoted with a structured memory tool when available, or with a standalone `Goal:`, `Decision:`, `Fact:`, `Constraint:`, `TODO:`, `Preference:`, `Failed:`,
`Failure:`, or `Lesson:` marker. Tests cover both sides of this boundary: unmarked information remains searchable but can be absent from resume; the same fact explicitly marked as `Fact:` is recovered after an equally long distractor tail.

The original evidence-presence gate is scoped to explicitly promoted evidence. Automatic extraction is evaluated separately against the versioned Russian corpus and is not a claim that arbitrary unmarked dialogue will be reconstructed semantically.

## What is still deployment-specific

The repository provides the runner protocol but does not ship a universal LLM judge. A meaningful
production suite must define real project tasks, isolated workspaces, deterministic reset,
side-effect policy, test oracles, model/version settings and cost accounting.
## 3. Performance and information-retention benchmark

The task runner measures downstream task quality. The separate performance benchmark measures
whether derived tool-output views pay for their own CPU/storage overhead and what information they
omit from the immediate prompt. Run:

```powershell
joiny-mnemonic-benchmark --project-root . --repetitions 100 `
  --prompt-exposures 10 --assert-gates
```

It executes real test/search/diff subprocesses, compares baseline and enriched SQLite stores, and
gates critical signals, path references and byte-exact source recovery. Methodology and metric
definitions are in [performance.md](performance.md).

## Russian automatic-extraction corpus

The versioned corpus evals/extraction_ru_v1.json is primarily Russian and covers goals,
decisions, facts, failures, lessons, negatives, anaphora, repetition, code zones, blockquotes,
rhetorical quotations, prompt injection, untrusted event kinds, private regions, ambiguous
evidence and malformed negatives.

The evaluate-extraction command reports precision, recall and F1 overall, by memory type and by
evidence zone, plus exact-evidence acceptance, false trusted records, quarantine rate, duplicate
rate and latency. Retry/backlog/storage behavior remains covered by deterministic integration and
benchmark tests.

Automatic extraction remains disabled by default. The v1 corpus is a labelled harness, not a
claimed production baseline. A release may enable extraction by default only after recording a
reviewed corpus version, pinned model/configuration hash, chosen threshold and explicit acceptable
precision/recall target. Adversarial false trusted/protected records have a hard target of zero.
Raw model confidence is used for routing and is not described as calibrated probability.
