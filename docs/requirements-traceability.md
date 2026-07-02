# Соответствие `goal.md`

Эта матрица связывает требования с реализацией и проверкой. Наличие строки — не замена запуску
теста; команда полного прогона приведена в README.

## Обязательные функции

| Требование | Реализация | Проверка |
|---|---|---|
| Append-only messages/tool calls/outputs/artifacts | `storage.py`: `events`, `artifacts`, `_append_event_in_tx` | `test_canonical_events_are_immutable_and_hash_chained` |
| Нет update/delete единственной source copy | SQL triggers `events_no_*`, `artifacts_no_*` | тот же тест выполняет прямые SQL UPDATE/DELETE |
| Версии и provenance производных записей | `block_versions`, `memory_records`, `snapshots` | `test_typed_memory_requires_and_returns_exact_provenance`, snapshot tests |
| Protected active blocks | `set_active_block`, `PromptAssembler.BLOCK_ORDER` | `test_active_blocks_are_versioned_and_never_compacted` |
| Последние сообщения verbatim | `PromptAssembler._event_text` | prompt tests и exact content assertions |
| Компактный index старой истории | historical index section, `RetrievalEngine.timeline` | prompt budget tests |
| Prompt под token budget | `PromptAssembler.assemble` | active block и resume tests |
| Index/state/summary/source | timeline, snapshot state, memory records, `exact_source` | provenance + snapshot tests |
| Optional embeddings/graph/KV | `PluginRegistry`, entry-point groups | capability tests |
| Time/text/file/type retrieval | FTS5/BM25 candidate SQL + `RetrievalEngine` rerank | `test_fts_retrieval_avoids_full_python_history_scan`, branch FTS test |
| Semantic retrieval | Optional plugin protocol only | `capabilities.semantic_retrieval` is false without a plugin |
| Exact source promotion | `promote_to_source`, `MemoryService.exact_source` | provenance test |
| Точное актуальное содержимое файла | `SnapshotManager.read_project_source` + root containment | snapshot/source tests |
| Query/freshness/risk/cost score | `_memory_hit` | retrieval unit path; параметры видны в metadata |
| Нет universal importance | score создаётся только из `RetrievalContext` | архитектурный инвариант |
| Atomic incremental snapshots | recursive `json-patch-v2` delta внутри SQLite transaction | nested-memory delta + parent materialization tests |
| Snapshot + replay tail | `SnapshotManager.restore` | stale/replay test |
| Parent/branch lineage | `branches`, fork cursor visibility | `test_branch_lineage_hides_parent_updates_after_fork` |
| Resume packet from restored state | `MemoryService.resume` passes materialized snapshot + tail to prompt | `test_resume_passes_materialized_snapshot_state_to_prompt` |
| Git HEAD/file hashes/staleness | `fingerprint_project`, `compare_fingerprints` | stale snapshot test |
| Независимое ядро + MCP/CLI/API | `MemoryService`, `mcp.py`, `cli.py`, `api.py` | MCP, stdio и HTTP tests |
| Claude/Codex/OpenCode/OpenHands capture | `hooks.py` runtime + project installers | four-agent core test, hook idempotency/installer tests |
| Complete tool interactions | atomic `append_events_once`, transcript grouping | `test_hook_retry_is_idempotent_and_tool_pair_is_atomic`, orphan-output test |
| Automatic sourced consolidation | `EvidenceConsolidator` explicit markers/candidates | `test_explicit_markers_create_sourced_memory_and_protected_blocks` |
| Active compaction continuity | extractive summary/index + lifecycle snapshot/reinjection | compaction provenance and session-hook tests |
| Python codegraph | AST symbols, calls, exact context, reverse impact | `test_python_ast_call_graph_context_and_reverse_impact` |
| Real task evaluation interface | `SubprocessTaskRunner`, `evaluate_with_runner` | task-runner/diagnostic separation test |
| Graceful capability model | `adapter_capabilities`, plugin error isolation | capability test |
| API text/recompute | prompt/retrieval + `TEXT_RECOMPUTE` | physical governor test |
| GPU/CPU KV, quantization, offload extension | `Placement`, `KVTier` protocol; no implementation shipped | capabilities + physical governor selection test |
| Store/recompute policy | `PhysicalMemoryGovernor.choose` | physical governor test |
| Retrieved data не instructions | `memory_as_untrusted_data` | prompt-injection test |
| Active instructions против compaction | hard budget error | active block test |
| Secret filtering before save | `SecretRedactor` before transaction | secret filter test |

## Исключённые решения

| Запрет | Доказательство дизайна |
|---|---|
| hard eviction canonical data | нет delete API; SQL delete triggers |
| generative exact-fact recovery | source promotion читает canonical events |
| обязательный knowledge graph | graph является optional plugin |
| attention как truth | score не использует attention |
| positional/semantic/attention super-DAG | lineage состоит только из branch/snapshot parent links |
| SVD/конкретный KV compressor в core | core содержит protocol + placement policy, не compressor |

## Критерии готовности

| Критерий | Авторитетное доказательство |
|---|---|
| Подтверждённое событие переживает abrupt exit | `CrashDurabilityTest` в стандартном прогоне |
| Startup state ≤1500 токенов | `MemoryService.resume` hard cap + resume tests |
| Active instructions сохраняются | active block version/prompt test + protected-size admission limit |
| Любое memory claim ведёт к source | FK-like validation + provenance test |
| Stale snapshots обнаруживаются | file hash mutation test |
| Evidence retention diagnostic | six-task reference suite after 120 distractor events; explicitly not task-level |
| Task-level quality gate | external runner report is required; diagnostic report is rejected | task-runner separation test |
| Одно ядро принимает события 4 agent families | adapter integration test |
| Hook configs сохраняют существующие handlers | installer merge/idempotency test |
| Full test command completes | 48 tests completed in the 2026-07-02 implementation run |

## Команды аудита

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v

joiny-mnemonic verify
```
## Added token-efficiency features

| Requirement | Implementation | Verification |
|---|---|---|
| Preserve exact tool output | Canonical `events`; immutable `tool_output_views` reference source hash | `test_reducer_preserves_raw_source_and_critical_failure_signals` |
| Never expand prompt with a view | Reducer stores prompt view only when framed view is smaller | benchmark `no_workload_expands_prompt` gate |
| Observe actual and estimated usage | Append-only `usage_samples`, provider/estimate flag, retry receipts | `test_hook_retries_do_not_double_count_reduction_or_provider_usage` |
| Trigger before context exhaustion | Versioned ratios and audited/rate-limited governor actions | `test_governor_uses_versioned_policy_and_applies_audited_actions` |
| Task-specific continuity | Task branch, protected goal, snapshot, versioned status, resume packet | task-boundary and native-task hook tests |
| Measure net benefit | Real subprocess corpus, baseline/enriched DB comparison, latency and storage | `test_real_subprocess_benchmark_enforces_profit_and_recovery_gates` plus checked benchmark report |
| Measure information loss | critical/path/verbatim recall and SHA-256 exact promotion | benchmark retention gates |
## Global hooks and early warning

| Requirement | Implementation | Verification |
|---|---|---|
| User-global hooks without fixed project path | env/OS-aware config resolution plus `hook --global` runtime Git-root discovery | `test_global_installers_resolve_user_paths_and_runtime_project` |
| Reject unsupported fake global integration | OpenHands global install raises with repository-local guidance | same test |
| Count context before reducer/native compaction | raw `UserPromptSubmit` and `PostToolUse` increments with immutable receipts | `test_raw_hook_counter_warns_before_native_compaction_and_is_idempotent` |
| Warn on the crossing tool result | audited `context_warning`, forced `PostToolUse` context injection | same test |
| Do not double-count retries | unique counter receipt and crossing-event replay behavior | same test |
| Bound counter overhead | O(1) latest-total read and atomic append | benchmark `hook_counter_p95_under_25ms` gate |
## Joiny-Mnemonic naming

| Requirement | Implementation | Verification |
|---|---|---|
| Distribution and CLI use the GitHub project name | `joiny-mnemonic`, `joiny-mnemonic-benchmark` and Python package `joiny_mnemonic` | `test_distribution_and_console_script_identity` plus CLI smoke test |
| Preserve existing durable state | new `.joiny-mnemonic` default with read-in-place fallback to `.llm-memory/memory.db` | `test_global_installers_resolve_user_paths_and_runtime_project` |
| Preserve installed extension compatibility | legacy `llm_memory.*` entry-point groups load before renamed groups | `test_renamed_plugin_groups_override_legacy_groups` |