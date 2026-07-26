# Отчёт по этапу 5

Дата локальной приёмки: 26 июля 2026 года.

Этап вводит отдельный детерминированный путь постфактум-финализации. Он не
использует семантический экстрактор и не повышает доверие обычного текста
ассистента.

## Реализация

- `src/joiny_mnemonic/finalization.py` проверяет точную грамматику и принимает
  теги только из сохранённого `host_assistant_finalization`.
- `CONFIRMED` атомарно создаёт память с уровнем `agent_finalized` и точным
  source event; `REJECTED` и `DEFERRED` остаются только в append-only аудите.
- `finalization_records` и `finalization_quarantine` добавлены схемой v11;
  миграция существующей базы остаётся backup-first.
- malformed, non-standalone и противоречивые статусы получают стабильный код
  карантина; повтор одной строки идемпотентен.
- обычные сообщения ассистента, их старые auto-memory и производные события
  исключены из resume, compaction и автоматических retrieval arms. Исходник
  по-прежнему доступен только через явный exact source/context.
- `AGENTS.md` и `CLAUDE.md` задают одинаковый рабочий контракт для обоих
  хостов; automatic extraction остаётся выключенным и независимым.

## Приёмочные проверки

Восемь проверок из `ROADMAP.md` находятся в
`tests/test_stage5_finalization.py`:

1. `test_01_confirmed_is_found_after_fresh_session_on_both_hosts`.
2. `test_02_unanswered_question_creates_no_memory`.
3. `test_03_unselected_proposal_creates_no_memory`.
4. `test_04_rejected_is_audited_but_not_an_active_decision`.
5. `test_05_deferred_is_audited_but_not_a_selected_decision`.
6. `test_06_forgeries_malformed_duplicates_and_conflicts_fail_closed`.
7. `test_07_finalization_does_not_leak_between_sibling_branches`.
8. `test_08_every_memory_resolves_to_the_exact_host_stop_event`.

Каждая из восьми проверок выполняет один и тот же сценарий отдельно через
`claude-code` и `codex`; чередования проверок между адаптерами больше нет.

Дополнительный
`test_finalization_and_host_capture_are_atomic` закрепляет rollback всей
операции при сбое между созданием памяти и записью аудита.

Отдельно через настоящий CLI entrypoint `joiny_mnemonic hook` были поданы
нативные `Stop` payload для `--agent claude-code` и `--agent codex` в новую
SQLite-базу; обе финализации вернулись последующим `resume`.

## Обязательные дефекты перед этапом 7

- `tests.test_native_failure_capture.NativeFailureCaptureTest.test_derived_event_inherits_saved_source_trace_context` закрепляет наследование сохранённых
  `session_id` и `origin_adapter` производным событием `derive_memory`.
- `tests.test_retrieval_fusion.FusionTest.test_dates_and_hex_words_do_not_activate_exact_identifier_abstention` закрепляет отсутствие abstention для даты
  `20260717` и hex-похожего слова `decafbad`.
- Существующий
  `test_opaque_identifier_query_abstains_from_semantic_neighbours` сохраняет
  fail-closed поведение для настоящего отсутствующего opaque ID.

## Результаты

```text
python -m unittest tests.test_stage5_finalization -v
Ran 9 tests — OK

python -m pytest -q
344 tests collected; 100% passed

ruff check <изменённые Python-файлы>
All checks passed!
```

Единственное предупреждение полного прогона относится к невозможности создать
`.pytest_cache`; на выполнение тестов оно не влияет.
