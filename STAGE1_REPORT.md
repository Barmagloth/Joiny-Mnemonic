# Отчёт о выполнении этапа 1

Дата: 21 июля 2026 года
Рабочая папка: `R:\Projects\Joiny-Mnemonic`

## Итог

Этап 1 из `ROADMAP.md` реализован полностью и подтверждён автоматическими проверками. К этапам 2–6, постфактум-финализации, экстрактору и dogfood переход не выполнялся.

В проекте введён общий механизм проверки переходов, который проверяет:

- допустимость перехода по таблице конкретной сущности;
- терминальное состояние;
- существование и branch visibility исходного события;
- provenance и trust, вычисленные из сохранённого source event;
- правила delegated origin;
- идемпотентность операции.

Workstream, Candidate, Finding и Settlement сохранили собственные таблицы переходов и собственную семантику. Универсальная таблица состояний для всех сущностей не создавалась.

## Исправления после post-stage аудита

После первоначальной приёмки был обнаружен блокер: терминальные переходы
Workstream требовали source_event_id, но публичные CLI, MCP и HTTP поверхности
не могли передать или законно создать такое доказательство. Блокер устранён:

- CLI task-status создаёт process-authored local_operator evidence;
- CLI task-reopen является отдельной командой с обязательной причиной;
- MCP memory_task_status принимает ID сохранённого source event;
- MCP memory_task_reopen является отдельным tool;
- HTTP status принимает ID сохранённого source event;
- HTTP /v1/tasks/{key}/reopen является отдельным endpoint;
- public/untrusted, отсутствующее и невидимое evidence по-прежнему отклоняется.

Также исправлены замечания «буква vs дух»:

- assistant finalization теперь требует совпадения origin_adapter с adapter
  evidence, проштампованным принимающей host boundary;
- producers modes получаются реальным прогоном ingress, а statuses привязаны
  к runtime-методам, сохраняющим переходы; ручной список modes удалён;
- отдельный policy trust-набор удалён из storage.py;
- threshold storage.py понижен до исторической исходной точки;
- внешний markdown fence вокруг ROADMAP.md удалён.

Дополнительно изменены api.py, mcp.py, cli.py и task_storage.py; исполняемые
round-trip проверки находятся в tests/test_workstream_surfaces.py.
## Проверка H1–H9

### Закрытые обходы H1–H7

- **H1:** прямой переход Candidate `rejected → confirmed` отклоняется.
- **H2:** Candidate в терминальном состоянии нельзя вернуть в `auto`.
- **H3:** `rejected` и `superseded` candidates исключены из автоматического повторного сопоставления.
- **H4:** повторное acknowledgement Finding идемпотентно и не создаёт новую запись перехода.
- **H5:** acknowledged Finding нельзя вернуть в `ack_request`.
- **H6:** обычный переход Workstream `cancelled → active` запрещён. Возобновление оформлено отдельной операцией reopen, требующей непустую причину, новый source event и доверенный origin.
- **H7:** переход Workstream `cancelled → completed` запрещён.

### Сохранённая строгость H8–H9

- **H8:** Settlement из недоверенного source event отклоняется.
- **H9:** delegated Settlement разрешается только при наличии действующего policy grant.

Существующая семантика Settlement, включая собственную таблицу переходов, доверенные локальные и host-user события, policy-authorized delegation и идемпотентность, сохранена.

## Provenance и trust

Поддерживаются только origin, определённые этапом 1:

- `external_untrusted`;
- `host_logical_user`;
- `host_assistant_finalization`;
- `local_operator`;
- `delegated_agent`;
- `bootstrap_tofu`.

Origin и trust вычисляются исключительно по сохранённому source event. Значение origin, переданное через CLI, MCP, HTTP, hook или внутренний вызов, не используется как доказательство.

`host_assistant_finalization` требует точного сочетания сохранённых признаков: host hook, роль assistant, событие Stop и непустой origin adapter.

Удалены не предусмотренные контрактом значения `signed_host_receipt` и `external_trusted_ui`, а также публичные capability/policy flags без реальных runtime consumers.

## Инварианты и gates

### JM-INV-001

Автоматические тесты перебирают все объявленные разрешённые переходы Workstream, Candidate и Finding, а также запрещённые обратные и межстатусные переходы.

Исполняемые проверки находятся в:

- `tests/test_stage1_transitions.py`;
- классах тестов разрешённых и запрещённых переходов, указанных в `ROADMAP.md`.

### JM-INV-004

Автоматически проверяются:

- игнорирование claimed origin;
- обязательное существование source event;
- branch visibility source event и целевой сущности;
- точные условия assistant finalization;
- получение trust только из сохранённого evidence.

### Contract gate

Команда:

```powershell
$env:PYTHONPATH='src'
python scripts/stage1_gates.py contract
```

Результат: **PASS**.

Негативная фикстура:

```powershell
python scripts/stage1_gates.py contract --inject-dead origins:dead_origin
```

Результат: ожидаемый **FAIL**, exit code `1`, сообщение об отсутствии producer для `dead_origin`.

Gate проверяет реальные producers и runtime consumers публичных origin, capability flags, policy flags, статусов и режимов.

### Complexity gate

Команда:

```powershell
$env:PYTHONPATH='src'
python scripts/stage1_gates.py complexity
```

Результат: **PASS**.

Фиксированный baseline хранится в quality/complexity-baseline.json. Порог storage.py после аудита необратимо понижен до исторического tracked HEAD: 4847 строк, 156 функций/методов и 4 класса. Текущий результат: 4832/146/4.

Совместный запуск:

```powershell
$env:PYTHONPATH='src'
python scripts/stage1_gates.py all
```

Результат: **PASS: stage1 all gate**.

## Результаты тестов

Focused stage-1 tests:

- **19/19 passed**.

Полный suite:

- **302/302 passed**;
- время финального запуска после включения observation-only dogfood: **758.466 s**.

Первый запуск полного suite внутри sandbox дал три инфраструктурные ошибки Windows `WinError 5`, связанные с системным TEMP/Git signal pipe. Повторный полный запуск вне sandbox прошёл без ошибок.

Дополнительные проверки:

- `git diff --check` — успешно;
- компиляционная проверка новых модулей — успешно.

## Изменённые для этапа 1 файлы

### Реализация

- `src/joiny_mnemonic/transition_rules.py`;
- `src/joiny_mnemonic/provenance.py`;
- `src/joiny_mnemonic/storage.py`;
- `src/joiny_mnemonic/tasks.py`;
- `src/joiny_mnemonic/adapters.py`;
- `src/joiny_mnemonic/policy_contract.py`;
- `src/joiny_mnemonic/service.py`;
- `src/joiny_mnemonic/dataflow_storage.py`;
- `src/joiny_mnemonic/storage_support.py`.

### Gates и документация

- `scripts/stage1_gates.py`;
- `quality/complexity-baseline.json`;
- `ROADMAP.md`;
- `STAGE1_REPORT.md`.

### Тесты

- `tests/test_stage1_transitions.py`;
- `tests/test_stage1_gates.py`;
- `tests/test_extraction.py`;
- `tests/test_integrations.py`;
- `tests/test_reduction_usage_governor_tasks.py`;
- `tests/test_telemetry.py`.

## Состояние рабочего дерева

До начала этапа 1 рабочее дерево уже содержало параллельные незакоммиченные и untracked изменения. Они были предварительно изучены и сохранены. Ничего чужого не откатывалось и не перезаписывалось.

Пересекающиеся изменения в `storage.py` и `service.py` были сохранены; существующая dataflow-реализация вынесена в отдельный mixin без изменения её внешнего поведения, чтобы выполнить зафиксированный complexity baseline.

Основная реализация этапа 1 сохранена локальным коммитом `01506a9`. Исправления post-stage аудита и observation-only dogfood также сохранены локально; push не выполнялся.

## Observation-only dogfood финализации

Ранний поведенческий сбор для риска этапа 5 включён без реализации самого
этапа 5:

- `AGENTS.md` и `CLAUDE.md` требуют от Codex и Claude Code строгие финальные
  теги только для действительно разрешённых итогов;
- `src/joiny_mnemonic/finalization_observer.py` читает сохранённые assistant
  `Stop`-события через SQLite `mode=ro`;
- `scripts/finalization_observe.py` считает валидные, отсутствующие,
  malformed и исключённые Markdown-lookalike строки отдельно по адаптерам;
- `tests/test_finalization_observer.py` доказывает неизменность event chain,
  memories, active blocks и Workstreams, а также hostile-грамматику;
- материализация, карантин, scoring и экстрактор не запускаются.

Focused observer tests: **5/5 passed**. Совмещённая focused-регрессия этапа 1
и наблюдателя: **20/20 passed**. Первый read-only срез по
`.joiny-mnemonic/memory.db`: **0** сохранённых доверенных assistant `Stop`
events; корпус начнёт наполняться следующими доставленными хуками.

## Следующий шаг

Dogfood публичного Workstream lifecycle готов. Observation-only observer и грамматика
финальных тегов готовы, но автоматическая доставка Codex `Stop`-событий ещё не
подтверждена живым событием; этапы
2–6, материализация финализаций и выбор экстрактора не начинались.

## Поправка dogfood-проверки 2026-07-22

Предыдущее утверждение о включённом автоматическом Codex-capture было неверным.
Исполняемые проверки установили:

- исходная строка Codex `[FACT] CONFIRMED: ... 0eebcfe` отсутствует среди
  assistant `Stop`-событий. Позднее Claude Code tool-output события внесли
  буквальные упоминания `0eebcfe`; это отдельные источники, не тот Codex-факт;
- observation-only observer видит **0** assistant `Stop`-событий Codex;
- до настройки отсутствовал `.codex/hooks.json`; после установки проектной
  конфигурации capability показывает `hooks_configured: true`, но одновременно
  `event_ingestion: false` и `hook_runtime_verified: false`;
- следовательно, наличие конфигурационного файла не считается доказательством
  доставки. Нужен следующий реально доставленный host event.
- текущий observer-срез после этой диагностики видит **2** настоящих
  `claude-code` assistant `Stop`-события и **7** валидных тегов; Codex
  `Stop`-событий всё ещё **0**. Это подтверждает Claude Code delivery, но не Codex.

Поиск по `0eebcfe` выявил независимый дефект: semantic arm возвращал ближайшие
элементы даже при отсутствии точного идентификатора, а cross-encoder только
переставлял их. Сырые rerank-score около `-11` подтверждали нерелевантность,
но модельно-зависимый порог не был частью контракта. Исправление
детерминированное: git-хэши и `evt_`/`mem_`/`op_`/`task_` идентификаторы в
запросе теперь являются точными лексическими ограничениями. Кандидат без
идентификатора отбрасывается.

Проверки:

- `python -m unittest tests.test_retrieval_fusion -v` — **11/11 passed**;
- реальный `search "0eebcfe" --limit 10` на проектной БД — пустая выдача;
- регрессионный тест доказывает обе стороны: отсутствующий ID → `[]`, событие
  с ID → возвращается ровно это событие;
- полный suite в Windows restricted-token sandbox: инфраструктурный сбой
  `%TEMP%` (`PermissionError`), не дефект продукта;
- повторный полный suite вне sandbox: **304/304 passed**, **350.105 s**;
- contract gate: **PASS**;
- complexity gate: **PASS**;
- `git diff --check` — ошибок нет.

Дэшборд перезапущен на `http://127.0.0.1:8766/` с исправленным retrieval-кодом.
## Codex hook trust audit 2026-07-22

После первого ответа с установленным `.codex/hooks.json` живой Codex `Stop`
снова не появился. Проверка свежего официального Codex manual и локального
Codex CLI `0.144.3` установила недостающую границу:

- lifecycle hooks — stable feature текущего Codex;
- новые или изменённые command hooks не запускаются до пользовательского
  review/trust точного определения;
- проектный hook дополнительно требует trusted project layer;
- интерактивный `/hooks` показал: **6 installed, 0 active, 6 review**;
- review ожидают `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`,
  `PreCompact`, `PostCompact`;
- автоматический `--dangerously-bypass-hook-trust` не использовался и trust за
  пользователя не подтверждался.

Диагностика продукта усилена: `install-hooks codex` теперь прямо сообщает, что
`hooks.json` доказывает только конфигурацию и требует `/hooks` review/trust;
`capabilities --agent codex` выдаёт тот же actionable warning, пока реальной
доставки нет. Focused-набор текущего дерева: **38/38 passed**; полный suite
вне Windows restricted-token sandbox: **304/304 passed**, **349.806 s**;
contract gate и frozen complexity gate: **PASS**.

Следующий обязательный acceptance-шаг требует явного пользовательского trust
этих шести hook definitions, затем нового Codex turn и проверки появления
assistant `Stop` в observation-only observer. До этого Codex dogfood не считается
подтверждённым.
## Codex dogfood подтверждён 2026-07-22

Пользователь явно разрешил доверять ровно шести command hooks из
`.codex/hooks.json`: `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`,
`PreCompact`, `PostCompact`. В интерактивном Codex review выбрано
`Trust all and continue`; автоматический `--dangerously-bypass-hook-trust` не
использовался. Живой интерфейс после review выполнил `SessionStart` и
`UserPromptSubmit`, что подтвердило применение trust.

Затем настоящий `codex exec` выполнил отдельный read-only turn и вернул ровно:

`[FACT] CONFIRMED: CODEX_HOOK_DOGFOOD_20260722_A captured a trusted Codex Stop event.`

Codex CLI сообщил успешное выполнение `SessionStart`, `UserPromptSubmit` и
`Stop`. Сохранённый Stop имеет следующие доказуемые атрибуты:

- event: `evt_fc1f0db11e564d8fa31cd9d192a0cfec`, seq `151`;
- `kind=message`, `role=assistant`;
- `origin_channel=host_hook`, `origin_adapter=codex`;
- сохранённый source payload содержит `hook_event_name=Stop` и исходный
  `last_assistant_message` с уникальным маркером;
- session: `019f8730-8876-72c1-9f63-738e8d0343f0`.

Исполняемые проверки после turn:

- `python scripts/finalization_observe.py --db .joiny-mnemonic/memory.db`:
  Codex — **1 Stop event, 1 event with valid tags, 1 valid tag**;
- тот же observer вернул `observation_only: true` и `materialized: false`;
- прямой запрос `memory_records` не нашёл ни provenance-ссылки на этот event,
  ни materialized-копии уникального маркера;
- `python -m joiny_mnemonic --db .joiny-mnemonic/memory.db --project-root . capabilities --agent codex`:
  `event_ingestion=true`, `hook_runtime_verified=true`,
  `hook_database_matches=true`, warnings отсутствуют;
- точный лексический поиск `CODEX_HOOK_DOGFOOD_20260722_A` вернул только две
  строки, где маркер действительно присутствует: входной `UserPromptSubmit`
  и выходной assistant `Stop`; нерелевантных semantic-кандидатов нет.

Итог: Codex command-hook capture и observation-only сбор финальных тегов готовы
к dogfood. Этапы 2–6, extractor и материализация финализаций не начинались.

### Финальная перепроверка после живого Codex turn

После runtime-проверки код продукта не изменялся. Повторно выполнены:

- `python scripts/stage1_gates.py all` — **PASS**;
- focused-набор `test_consolidation_and_hooks`, `test_dataflow`,
  `test_integrations`, `test_plugins_and_integrity`,
  `test_retrieval_fusion` — **49/49 passed**, **87.716 s**;
- `git diff --check` — ошибок нет, только предупреждения Git о будущей
  нормализации LF/CRLF.

Последний полный suite на том же коде продукта: **304/304 passed**,
**349.806 s**. После него менялись только данный отчёт и локальное runtime
trust-состояние Codex.

## Исправление текущих residual risks 2026-07-22

Закрыты два текущих dogfood-дефекта из `stage1-audit-residual-risks.md` без
перехода к этапам 2–6:

- Historical Index packet содержит budget-aware подсказку
  `joiny-mnemonic source <id>`;
- generic `Exit code N` больше не становится durable failure-memory;
  содержательные автоматические failures имеют `auto` authority;
- существующие append-only generic failure-записи сохранены, но исключены из
  retrieval и Historical Index.

Реальная БД возвращает `[]` для generic failure-поиска без events/semantic.
Focused tests: **26/26**. Полный suite: **305/305**, **343.244 s**.
Contract и frozen complexity gates: **PASS**; baseline не пересчитывался.
