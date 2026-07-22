# Отчёт по этапу 2 — неделимые действия

Дата приёмки: 2026-07-22.

Статус: этап 2 завершён; к этапу 3 работа не переходила.

## Карта требований

| Требование этапа 2 | Реализация | Исполняемая проверка | Итог |
|---|---|---|---|
| Создание Workstream: ветка, событие, goal, snapshot, версия | re-entrant transaction boundary охватывает `TaskManager.start` | `test_jm_inv_002_task_start_rolls_back_after_every_step` | старое либо полностью новое состояние |
| Изменение статуса: событие, snapshot, версия | `TaskManager.set_status` и operator path выполняются атомарно | `test_jm_inv_002_task_status_rolls_back_after_every_step` | частичных версий нет |
| Подтверждение кандидата: событие, переходы, link, memory | quarantined и auto candidate проходят один atomic confirmation path | `test_jm_inv_002_candidate_confirmation_rolls_back_every_step`, `test_jm_inv_002_quarantined_confirmation_materializes_atomically`, `test_candidate_confirmation_rolls_back_its_host_event` | подтверждение материализует память и откатывается целиком |
| Apply/revert согласованного изменения | settlement и reconciler используют общую внешнюю транзакцию | `test_jm_inv_002_settlement_apply_rolls_back_every_step`, `test_jm_inv_002_settlement_revert_rolls_back_every_step` | событие, переход и блок неделимы |
| Completion проверяет Obligations | учитываются строки `open_tasks` и видимые неприменённые `task_closure` | `test_settlement_changes_workstream_obligations`, `test_completion_fails_before_write_and_exact_override_is_audited` | без exact override запись не начинается |
| Override доступен публично | exact IDs и непустая причина проходят через CLI, MCP и HTTP | `test_obligation_override_is_reachable_on_all_status_surfaces` | одинаковый контракт трёх поверхностей |
| Derived systems после core commit | semantic, graph, extraction wakeup и witness имеют durable failure/retry ledger | оба теста `test_jm_inv_008_*` | core сохраняется, отказ видим и повторяем |

Пробелов по приёмке этапа 2 не осталось.

## Существенные решения

- Nested write-методы присоединяются к одной внешней SQLite-транзакции; внутренний сбой помечает всю границу на rollback.
- Post-commit callback не входит в core transaction.
- Ошибки производных систем записываются каноническими событиями `derived_projection_failed`; успешный retry — `derived_projection_recovered`.
- Override завершения допустим только при точном совпадении текущих obligation IDs и непустой причине. Оба значения сохраняются в событии перехода.
- Общий transition validator этапа 1 сохранён; универсальная FSM или новая таблица переходов не создавались.
- `task.md` помечен как `SUPERSEDED_BY: ROADMAP.md + TODO.md §0`; его исторические концепты не использовались как нормативные требования.

## Результаты проверок

- Stage-2 fault suite: 11/11, PASS.
- Focused regression suite: 69/69, PASS.
- Полный suite вне ограниченного sandbox: 317/317, PASS за 373.970 с.
- Первый полный прогон внутри sandbox: 314 успешных, 2 errors и 1 failure из-за запрета `%TEMP%` и Git for Windows (`Win32 error 5`); неизменённый повтор вне sandbox прошёл 317/317.
- Contract/complexity gates: `PASS: stage1 all gate`.
- Baseline не пересчитывался и не повышался.
- `git diff --check`: PASS.

Точные команды закреплены в разделе приёмки этапа 2 в `ROADMAP.md`.

## Изменённые файлы этапа 2

- `src/joiny_mnemonic/transactions.py`
- `src/joiny_mnemonic/projection_failures.py`
- `src/joiny_mnemonic/candidate_confirmation.py`
- `src/joiny_mnemonic/storage.py`
- `src/joiny_mnemonic/storage_support.py`
- `src/joiny_mnemonic/tasks.py`
- `src/joiny_mnemonic/settlement.py`
- `src/joiny_mnemonic/reconciler.py`
- `src/joiny_mnemonic/service.py`
- `src/joiny_mnemonic/hooks.py`
- `src/joiny_mnemonic/consolidation.py`
- `src/joiny_mnemonic/cli.py`
- `src/joiny_mnemonic/mcp.py`
- `src/joiny_mnemonic/api.py`
- `tests/test_stage2_atomicity.py`
- `tests/test_workstream_surfaces.py`
- `ROADMAP.md`
- `task.md`
- `STAGE2_REPORT.md`

Параллельные изменения в `TODO.md`, `benchmarks/results/census-latest.json`,
`src/joiny_mnemonic/reducers.py`, `task7.md`, `README2.md`, `ROADMAP_old.md` и
`benchmarks/results/census-codex-rejudge-20260717/` не откатывались и не
перезаписывались.

## Что остаётся вне этапа 2

Два ранее зафиксированных dogfood-дефекта не маскируются этим отчётом:
атрибуция `derive_memory` должна наследовать `session_id` и `origin_adapter`
из сохранённого source event, а exact-ID abstention не должен принимать дату
или hex-похожее обычное слово за opaque ID. Их обязательные регрессии уже
закреплены в `ROADMAP.md` до начала зачётного dogfood-периода.
