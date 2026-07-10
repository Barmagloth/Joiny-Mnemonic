# Соответствие `goal.md`

Эта матрица связывает требования с реализацией и проверкой. Наличие строки — не замена запуску
теста; команда полного прогона приведена в README.

## Обязательные функции

| Требование | Реализация | Проверка |
|---|---|---|
| Append-only messages/tool calls/outputs/artifacts | `storage.py`: `events`, `artifacts`, `_append_event_in_tx` | `test_canonical_events_are_immutable_and_hash_chained` |
| Нет update/delete единственной source copy | SQL triggers `events_no_*`, `artifacts_no_*` | тот же тест выполняет прямые SQL UPDATE/DELETE |
| Automatic integrity rejection | startup verification, read version guard, MCP pre-dispatch verification | `AutomaticIntegrityTest` corrupts a canonical row |
| Версии и provenance производных записей | `block_versions`, `memory_records`, `snapshots` | `test_typed_memory_requires_and_returns_exact_provenance`, snapshot tests |
| Protected active blocks | `set_active_block`, `PromptAssembler.BLOCK_ORDER` | `test_active_blocks_are_versioned_and_never_compacted` |
| Последние сообщения verbatim | `PromptAssembler._event_text` | prompt tests и exact content assertions |
| Компактный index старой истории | historical index section, `RetrievalEngine.timeline` | prompt budget tests |
| Prompt под token budget | `PromptAssembler.assemble` | active block и resume tests |
| Index/state/summary/source | timeline, snapshot state, memory records, `exact_source` | provenance + snapshot tests |
| Optional embeddings/graph/KV | `PluginRegistry`, entry-point groups; semantic and graph packages shipped separately | plugin behavior and capability tests |
| Time/text/file/type retrieval | FTS5/BM25 candidate SQL + `RetrievalEngine` rerank | `test_fts_retrieval_avoids_full_python_history_scan`, branch FTS test |
| Semantic retrieval | `plugins/semantic-local` indexes memories and canonical events with cosine search | `test_semantic_plugin_finds_unmarked_event_without_keyword_overlap` |
| Knowledge graph projection | `plugins/knowledge-graph` plus CLI/MCP/HTTP query surfaces | `test_knowledge_graph_is_queryable_and_branch_scoped` |
| Exact source promotion | `promote_to_source`, `MemoryService.exact_source` | provenance test |
| Точное актуальное содержимое файла | `SnapshotManager.read_project_source` + root containment | snapshot/source tests |
| Query/freshness/risk/cost score | `_memory_hit` | retrieval unit path; параметры видны в metadata |
| Per-memory Git staleness | on-demand `StalenessService`, CLI `stale`, optional search metadata | temporary-Git staleness tests and ranking-equivalence assertion |
| Нет universal importance | score создаётся только из `RetrievalContext` | архитектурный инвариант |
| Atomic incremental snapshots | recursive `json-patch-v2` delta внутри SQLite transaction | nested-memory delta + parent materialization tests |
| Snapshot + replay tail | `SnapshotManager.restore` | stale/replay test |
| Parent/branch lineage | `branches`, fork cursor visibility | `test_branch_lineage_hides_parent_updates_after_fork` |
| Resume packet from restored state | `MemoryService.resume` passes materialized snapshot + tail to prompt | `test_resume_passes_materialized_snapshot_state_to_prompt` |
| Git HEAD/file hashes/staleness | `fingerprint_project`, `compare_fingerprints` | stale snapshot test |
| Независимое ядро + MCP/CLI/API | `MemoryService`, `mcp.py`, `cli.py`, `api.py` | MCP, stdio и HTTP tests |
| Claude/Codex/OpenCode/OpenHands capture | `hooks.py` runtime + project installers | four-agent core test, hook idempotency/installer tests |
| Complete tool interactions | atomic `append_events_once`, transcript grouping, explicit failure derivation | success-pair, orphan-output and `test_native_failure_capture` tests |
| Automatic sourced consolidation | `EvidenceConsolidator` explicit markers/candidates including failure/lesson | marker, trust-policy and `test_failure_lesson` tests |
| Active compaction continuity | extractive summary/index + lifecycle snapshot/reinjection | compaction provenance and session-hook tests |
| Python codegraph | AST symbols, calls, exact context, reverse impact | `test_python_ast_call_graph_context_and_reverse_impact` |
| Real task evaluation interface | `SubprocessTaskRunner`, `evaluate_with_runner` | task-runner/diagnostic separation test |
| Graceful capability model | `adapter_capabilities`, plugin error isolation | capability test |
| API text/recompute | prompt/retrieval + `TEXT_RECOMPUTE` | physical governor test |
| GPU/CPU KV, quantization, offload extension | `Placement`, `KVTier` protocol; no implementation shipped | capabilities + physical governor selection test |
| Store/recompute policy | `PhysicalMemoryGovernor.choose` | physical governor test |
| Retrieved data не instructions | `memory_as_untrusted_data` | prompt-injection test |
| Active instructions против compaction | hard budget error | active block test |
| Secret/private-region filtering before save | `SecretRedactor` before transaction | secret filter and `test_private_regions` surface tests |

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
| Invalid host JSON is never overwritten | pre-validation before limits/config writes | `test_invalid_claude_json_is_rejected_without_partial_install` |
| Valid host JSON is recoverable | verified `.joiny-mnemonic.bak`, post-write validation and rollback | `test_failed_claude_write_restores_verified_backup` |
| Full test command completes | 62 tests completed in the 2026-07-03 implementation run |

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
| Trigger before context exhaustion | Per-agent JSON profiles, absolute handoff cap, reserve and audited/rate-limited actions | governor policy test plus `test_seven_bundled_profiles_and_agent_specific_install_config` |
| Task-specific continuity | Task branch, protected goal, snapshot, versioned status, resume packet | task-boundary and native-task hook tests |
| Measure net benefit | Real subprocess corpus, baseline/enriched DB comparison, latency and storage | `test_real_subprocess_benchmark_enforces_profit_and_recovery_gates` plus checked benchmark report |
| Measure information loss | critical/path/verbatim recall and SHA-256 exact promotion | benchmark retention gates |
## Global hooks and context handoff

| Requirement | Implementation | Verification |
|---|---|---|
| User-global hooks without fixed project path | env/OS-aware config resolution plus `hook --global` runtime Git-root discovery | `test_global_installers_resolve_user_paths_and_runtime_project` |
| MCP does not imply automatic capture | first-connect warning plus configured/runtime capability fields | `test_capabilities_and_mcp_distinguish_installer_from_active_hooks` |
| Keep MCP and hooks on one project database | host project environment plus project-relative database resolution and explicit path diagnostics | `test_claude_mcp_relative_paths_follow_claude_project_dir` |
| Accept native PowerShell hook input | raw stdin decoding with optional UTF-8 BOM before JSON parsing | `test_hook_cli_accepts_utf8_bom_from_powershell_pipe` |
| Invalid hook config is visible | `hook_configuration_status=invalid-config` and dependent capabilities false | `test_capabilities_report_invalid_claude_settings` |
| Reject unsupported fake global integration | OpenHands global install raises with repository-local guidance | same test |
| Configure different agents independently | project/global `context-limits.json` with seven model presets and explicit overrides | `test_seven_bundled_profiles_and_agent_specific_install_config` |
| Count context before reducer/native compaction | raw `UserPromptSubmit` and `PostToolUse` increments with immutable receipts | `test_raw_hook_counter_warns_before_native_compaction_and_is_idempotent` |
| Agent-assisted durable promotion | explicit trust policy: user records/blocks, assistant records only, external kinds neither | `test_trust_policy`, session-hook instruction test, and `test_agent_marker_in_stop_hook_is_promoted` |
| Checkpoint without premature handoff | audited `context_checkpoint`; recommendation starts only at handoff | `test_snapshot_checkpoint_does_not_recommend_handoff_early` |
| Do not double-count retries | unique counter receipt and crossing-event replay behavior | same test |
| Bound counter overhead | O(1) latest-total read and atomic append | benchmark `hook_counter_p95_under_25ms` gate |
## Joiny-Mnemonic naming

| Requirement | Implementation | Verification |
|---|---|---|
| Distribution and CLI use the GitHub project name | `joiny-mnemonic`, `joiny-mnemonic-benchmark` and Python package `joiny_mnemonic` | `test_distribution_and_console_script_identity` plus CLI smoke test |
| Preserve existing durable state | new `.joiny-mnemonic` default with read-in-place fallback to `.llm-memory/memory.db` | `test_global_installers_resolve_user_paths_and_runtime_project` |
| Preserve installed extension compatibility | legacy `llm_memory.*` entry-point groups load before renamed groups | `test_renamed_plugin_groups_override_legacy_groups` |
