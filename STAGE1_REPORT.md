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

Dogfood публичного Workstream lifecycle и observation-only сбор финальных
тегов готовы. Продолжать собирать реальные `Stop`-события обоих хостов; этапы
2–6, материализация финализаций и выбор экстрактора не начинались.
