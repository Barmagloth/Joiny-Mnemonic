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

Фиксированный baseline хранится в `quality/complexity-baseline.json` и не пересчитывается от текущего разросшегося дерева.

Совместный запуск:

```powershell
$env:PYTHONPATH='src'
python scripts/stage1_gates.py all
```

Результат: **PASS: stage1 all gate**.

## Результаты тестов

Focused stage-1 tests:

- **14/14 passed**.

Полный suite:

- **294/294 passed**;
- время финального запуска: **341.830 s**.

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

Staging, commit и push не выполнялись.

## Следующий шаг

Работа остановлена на границе этапа 1. Dogfood может быть начат только отдельным следующим действием после принятия этого отчёта и результатов этапа 1.
