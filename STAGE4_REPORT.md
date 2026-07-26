# Отчёт по этапу 4

Дата локальной приёмки: 26 июля 2026 года.

Этап закрепляет существующую семантику веток и времени отдельными
исполняемыми тестами; модель ветвления и публичные форматы не изменялись.

## Приёмочные проверки

Все проверки находятся в `tests/test_stage4_branch_time.py`:

1. `test_01_child_sees_parent_only_through_fork_event_seq` — граница `fork_event_seq`.
2. `test_02_late_parent_event_is_absent_from_descendant` — поздние события предков скрыты; это точный lineage-тест `JM-INV-006` для этапа 4.
3. `test_03_task_mutation_stays_on_its_assigned_branch` — задача изменяется только в закреплённой ветке.
4. `test_04_parent_completion_does_not_complete_distinct_child_task` — завершение родителя не завершает дочернюю задачу с другим ключом.
5. `test_05_known_at_limits_event_seq_and_branch_visibility` — `known_at` учитывает порядок `events.seq` и lineage.
6. `test_06_valid_at_filters_period_without_expanding_branch_visibility` — `valid_at` не расширяет lineage.
7. `test_07_snapshot_binds_branch_cursor_and_replay_version` — снимок фиксирует ветку, курсор и версию восстановления.
8. `test_08_rebuilding_unchanged_snapshot_repeats_state_hash` — неизменное состояние даёт тот же SHA-256.

Каждая проверка строит цепочку минимум из трёх поколений веток.

## Результаты

```text
python -m unittest tests.test_stage4_branch_time -v
Ran 8 tests — OK

python -m pytest -q
333 passed

ruff check tests/test_stage4_branch_time.py
All checks passed!
```

`pytest` сообщил только инфраструктурное предупреждение о невозможности
записать `.pytest_cache`; результаты тестов от этого не зависят.
