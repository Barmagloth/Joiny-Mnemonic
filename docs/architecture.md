# Архитектура

## Инварианты

1. `events` и `artifacts` — канонический источник истины.
2. Каноническая запись проходит secret redaction до начала durable transaction.
3. Успешный `append_event` означает завершённый `COMMIT` при `synchronous=FULL`.
4. Канонические строки нельзя обновить или удалить: это запрещено SQL-триггерами.
5. Active blocks, memory records и snapshots только добавляют версии.
6. Любое утверждение `MemoryRecord` содержит существующие `source_event_ids`.
7. Summary не заменяет source. Promotion всегда читает точное событие.
8. Retrieved memory — данные. Инструкциями считаются только явно активированные блоки.

## Слои данных

| Слой | Представление | Назначение |
|---|---|---|
| Canonical | `events`, `artifacts` | точный replay и аудит |
| Active | latest `block_versions` | инструкции, цель, ограничения, решения, задачи |
| Structured | `memory_records` | facts/decisions/tasks/preferences/failures/lessons и provenance |
| Index | timeline + `index` records | дешёвая навигация по старой истории |
| Summary/detail | поля memory record | progressive disclosure |
| Snapshot | parent delta + cursor | быстрый resume и lineage |
| Physical | plugin KV tiers | GPU/CPU/offload без зависимости ядра от компрессора |

## Транзакции и аварийность

Каждый append выполняется внутри `BEGIN IMMEDIATE … COMMIT`. Предпочитается WAL. Если
filesystem не поддерживает WAL locks, ядро использует rollback journal `DELETE` с exclusive
locking; `synchronous=FULL` сохраняется в обоих режимах. Hash chain вычисляется по
каноническому JSON и предыдущему chain hash. Артефакт и его событие записываются одной
транзакцией. Метод не подтверждает запись до завершения commit.

Это защищает от потери событий, уже подтверждённых приложением, в пределах гарантий SQLite и
нижнего storage stack. Повреждение/утрата всего носителя требует внешней резервной копии и не
маскируется генеративным восстановлением.

## Ветвление

`branches(parent_id, fork_event_seq)` образует простой lineage DAG. Видимость дочерней ветки:

- собственные события — полностью;
- события каждого предка — только до соответствующего fork cursor;
- block/memory/snapshot версии предка — только если их cursor не позже fork.

Обновления родителя после fork не протекают в дочерний контекст.

## Snapshots

Snapshot содержит:

- recursive JSON-patch delta относительно parent snapshot; изменение одной memory не сохраняет
  заново весь словарь memories/index;
- cursor последнего видимого canonical event;
- parent ID и branch ID;
- Git HEAD;
- SHA-256 выбранных или Git-tracked файлов.

Восстановление materialize-ит цепочку delta и применяет canonical tail после cursor. Перед
resume текущий fingerprint сравнивается с сохранённым; изменения Git HEAD, root или файлов
возвращаются как `stale_reasons`.

## Retrieval

Встроенный retrieval получает кандидатов через SQLite FTS5/BM25 и применяет фильтры времени,
файла, branch и типа внутри SQL/lineage-проверки. Python не сканирует полную историю при
непустом FTS-запросе; dependency-free scan остаётся fallback для SQLite без FTS5. Плагины могут
добавить semantic results. Итоговый score вычисляется для каждого кандидата:

```text
score = relevance*wq + freshness*wf + risk*wr + cost_efficiency*wc
```

Веса и half-life входят в `RetrievalContext`; постоянного универсального importance score нет.

## Prompt budget

Порядок включения:

1. protected active blocks;
2. последние transcript events дословно;
3. компактный исторический index;
4. релевантные summaries/detail как untrusted data.

Если active blocks не помещаются, сборка завершается ошибкой вместо compaction. Стандартный
resume ограничен 1500 оценочными токенами. Для production можно передать model-specific token
counter; встроенный estimator намеренно консервативен.

Чтобы одновременно гарантировать сохранность всех active instructions и стартовый лимит,
хранилище не принимает protected set больше 3000 UTF-8 bytes суммарно. Большую точную спецификацию
нужно оставить canonical/archival source, а в active block поместить краткую ссылку и цель.

## Consolidation и active-session compaction

`EvidenceConsolidator` применяет единый trust policy до разбора structured candidates и маркеров.
User message может создавать evidence-bound records и protected blocks. Assistant message может
создавать только searchable records. `tool_call`, `tool_output`, `artifact`, `state`,
`memory_block` и retrieved data не создают records или blocks из-за маркеров либо crafted
`payload.memory_candidates`. Явные `derive` и `block-set` остаются отдельными write API.
Маркеры `Failed:`/`Failure:` создают `failure`, а `Lesson:` создаёт `lesson`; эти типы не
изменяют protected blocks. Неявный LLM extraction в core отсутствует.

`MemoryService.resume` also injects a protected `[DURABLE MEMORY CAPTURE]` contract. It delegates the semantic judgment to the active agent: promote durable, evidence-backed information deliberately; leave transient or speculative prose unmarked. This preserves provenance without pretending that deterministic consolidation understands arbitrary dialogue.

Compaction выбирает полные transcript interaction groups, оставляет последние группы verbatim и
создаёт extractive `summary` + `index` со списком точных source event IDs. Hook runtime вызывает
consolidation, compaction и snapshot на lifecycle events, а затем передаёт resume packet в
native context-injection API агента. Canonical transcript при этом не переписывается.

`PostToolUse` доставляется как одна транзакция из `tool_call` и `tool_output`. Для Claude Code
`PostToolUseFailure` использует ту же atomic pair/receipt схему и после canonical commit создаёт
один deterministic `failure` с provenance обоих событий. Повторы native hook подавляются
immutable receipts. Resume никогда не начинает tool interaction с orphan
output.

## Code context

`PythonCodeIndex` строит live AST index по `.py` файлам, хранит symbols, resolved/unresolved call
edges и import edges, возвращает точный source span и traverses reverse callers для impact
analysis. Индекс кэшируется в процессе по file size/mtime. Другие языки не поддерживаются.

## Evaluation boundary

Evidence-presence — только regression diagnostic. Task-level evaluation существует как внешний
JSON subprocess runner: одна задача запускается с full-history и resume context, а quality gate
использует фактический runner score. Встроенного универсального LLM judge нет.

## Plugins

Entry-point группы:

- `joiny_mnemonic.semantic` — embeddings/vector retrieval;
- `joiny_mnemonic.knowledge_graph` — graph projection;
- `joiny_mnemonic.kv_tier` — физический KV storage.

Legacy `llm_memory.*` entry-point groups are loaded first for compatibility; renamed `joiny_mnemonic.*` plugins take precedence by plugin name.

Ядро оставляет тяжёлые зависимости опциональными, но репозиторий поставляет две отдельные
реализации:

- `plugins/semantic-local` — локальный sentence-transformer, persistent SQLite vector index,
  cosine retrieval по typed memories и обычным canonical events;
- `plugins/knowledge-graph` — persistent SQLite projection явных и маркированных entity
  relations с `memory_id`, `source_event_ids` и branch-aware filtering.

`MemoryService` передаёт plugin factory проектный root и путь канонической БД. Derived indexes
хранятся отдельно под `.joiny-mnemonic/plugins/`, могут быть перестроены из канонических данных
и никогда не становятся source of truth. Graph доступен через CLI `graph-neighbors`, MCP
`memory_graph_neighbors` и HTTP `POST /v1/graph/neighbors`. KV tier, quantizer и offloader
остаются только extension protocols.

Ошибка plugin не отменяет уже подтверждённую запись ядра и попадает в `plugin_errors`.
Отсутствующий plugin просто отключает соответствующую capability.
## Tool-output views, usage, governor and task lineage

`events(kind=tool_output)` remains the only authoritative output. `tool_output_views` contains
immutable, versioned-by-reducer derived representations with the source event hash, view hash,
raw/view sizes, token estimates and reducer latency. Prompt assembly selects the compact view only
when it is smaller; `exact_source(view_id)` always promotes to the canonical event.

`usage_samples` is append-only and distinguishes provider-reported values from local estimates.
Hook receipts also key usage and reduction records, so native retries do not double-count cost or
savings.

Per-agent limits live in the reviewable `.joiny-mnemonic/context-limits.json` file, with a global
file as fallback. Bundled model profiles separate advertised context capacity from the conservative
absolute handoff cap. Legacy versioned `budget_policies` remain a fallback for callers without an
agent profile. `BudgetGovernor` prefers provider-reported usage, then the raw hook counter, then a
canonical-history estimate. Snapshot, compact and handoff actions are rate-limited per resolved
policy and written to `governor_actions` before execution.

A task maps to one immutable branch lineage. `task_versions` records status transitions and
checkpoint snapshot IDs; `task_session_bindings` prevents a native session from silently moving
between tasks. Task resume uses the same snapshot-plus-tail prompt path and the same 1500-token
hard cap.
## Global hook resolution and pre-compaction counter

Global installers write only user-level host configuration. Their command has no fixed project or
database path. `hook --global` resolves the native payload's cwd/workspace to the nearest Git root
and opens `<root>/.joiny-mnemonic/memory.db`, preserving project isolation. Existing `.llm-memory/memory.db` stores remain readable through an explicit legacy-path fallback; no database is silently copied or deleted.

`hook_context_counters` is the append-only per-session counter. Each atomic increment stores
the new cumulative total, so reads remain constant-time as a session grows. It counts
raw `UserPromptSubmit` and `PostToolUse` payloads before tool-output reduction; receipt uniqueness
makes retries idempotent. The governor uses `max(provider_context, hook_cumulative)` and falls back
to raw canonical events, never compact views. Crossing the per-agent snapshot threshold emits one
audited `context_checkpoint` and returns the same checkpoint for a retry of the crossing delivery.
A handoff recommendation is not emitted until the separate handoff threshold.
