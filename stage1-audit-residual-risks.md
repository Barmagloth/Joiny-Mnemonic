---
name: stage1-audit-residual-risks
description: Итог аудита этапа 1 Roadmap (2026-07-22) — принят; два названных остаточных риска для этапов 3/5 и миграции
metadata:
  node_type: memory
  type: project
  originSessionId: e7e2d48c-909e-409b-abbc-7cc1764b2c4b
  modified: 2026-07-21T23:59:00.071Z
---

Этап 1 Roadmap (transition contracts) проверен и принят 2026-07-22: gates, негативные фикстуры, 297/297 тестов подтверждены независимыми прогонами. Блокер dogfood (недостижимость complete/cancel/reopen с поверхностей) исправлен: CLI чеканит `local_operator` evidence, MCP/HTTP принимают сохранённый `source_event_id`, HTTP отдаёт 403.

Остаточные риски, названные при приёмке (не блокеры, но их закрытие ожидается позже):

1. **Trusted-but-unrelated evidence**: агент через MCP/HTTP может завершить задачу, процитировав любое видимое host-user событие, даже не относящееся к завершению. Семантическая привязка evidence к действию — территория этапов 3/5.
2. **Легаси-финализация**: старые host-Stop события без штампа `_joiny_origin_adapter` в payload больше не выводят `host_assistant_finalization`. Потребителя до этапа 5 нет, но это надо учесть в проверке миграции старой базы (раздел 12 ROADMAP).

Также при dogfood: `completed` от агента означает «агент указал на настоящее сообщение пользователя», не «пользователь одобрил завершение».

Observation-only dogfood финализационных тегов запущен (коммит 0eebcfe, проверен 2026-07-22, 302/302): `finalization_observer.py` + `scripts/finalization_observe.py`, грамматика Roadmap 8.1, read-only mode=ro, без материализации. Инструкции хостам — AGENTS.md/CLAUDE.md, идентичность закреплена тестом. Открытый эмпирический пункт: корпус пуст (0 assistant-Stop событий); на первом непустом срезе проверить, что `content` Stop-событий не пустой — hooks берут его из `last_assistant_message`/`message`, и если хост не шлёт текст, все события уйдут в `untagged`, маскируя реальную эмиссию тегов.

2026-07-22: hooks для claude-code установлены в проект (`.claude/settings.json`, setup --without-mcp; до этого был только `.codex/hooks.json`, и Stop-события от Claude Code вообще не захватывались). Smoke-test на временной БД прошёл (Stop-событие записано, exit 0). Нюанс запуска CLI: `python -m joiny_mnemonic.cli` молча no-op (нет `__main__`-гарда), рабочие пути — `python -m joiny_mnemonic` (есть `__main__.py`) или entry point.

2026-07-22, probe-эксперимент «что знает следующая сессия»: вопрос про источники прошёл отлично (сессия корректно разделила файловую память Claude Code и packet Joiny, цитировала verbatim, сама назвала drift-риск). Вопрос «процитируй финальное сообщение прошлой сессии» вскрыл **дыру в recall-поверхности**: historical index обрезает content, packet не говорит, КАК развернуть событие; при setup --without-mcp memory-tools в сессии нет, и агент полез в raw sqlite с угаданной (неверной) колонкой `event_id` вместо `id` — та же ошибка, что делали мы, и каждый такой промах плодит новую junk failure-память. Легитимный путь существует и работает: `joiny-mnemonic source <evt_id...>` возвращает событие целиком verbatim. Продуктовый фикс: packet должен сам рекламировать эту команду одной строкой (chip создан).

2026-07-22, проверка первых реальных событий (закрыт открытый эмпирический пункт): первый настоящий Stop от Claude Code записан (seq 45, ses_b361f98f, `hook_event_name: "Stop"`), `content` НЕ пустой — полное финальное сообщение ассистента с кириллицей без искажений и с финализационными тегами verbatim. Записанное ранее утверждение «Claude Code снапшотит hooks при старте, установочная сессия не захватывается» ОПРОВЕРГНУТО: установочная сессия захватывалась с момента одобрения settings (события с 23:26:17, включая её Stop). Замеченный шум dogfood: derive_memory создаёт failure-память «Bash/PowerShell failed: Exit code 1» из любых разведочных команд с ненулевым exit (authority_level=confirmed), эти derived-события теряют session_id/origin_adapter (None) и затем ретривятся в промпты как мусор.

## Статус текущих dogfood-дефектов — исправлено 2026-07-22

Текущие, не отложенные к этапам 3/5 или миграции, пункты закрыты:

- MEMORY PACKET теперь рядом с Historical Index рекламирует budget-aware путь
  `joiny-mnemonic source <id>` для verbatim-разворачивания `evt_`/`mem_`;
- низкоинформативное `<tool> failed: Exit code N` остаётся в канонической
  паре tool_call/tool_output, но больше не материализуется как failure-memory;
- содержательные автоматические tool failures получают существующие
  `origin=auto` и `authority_level=auto`, а не `confirmed`;
- старые generic failure-memory и их derivation events не удалялись из
  append-only хранилища, но исключены из retrieval и Historical Index;
  `source <id>` по-прежнему раскрывает их provenance;
- реальная проектная БД: поиск `PowerShell failed Exit code 1` с
  `--no-events --no-semantic` возвращает `[]`; packet содержит подсказку
  `source <id>` и не содержит generic derived-failure строк.

Остаются отложенными согласно Roadmap: semantic binding trusted evidence к
конкретному Workstream и миграционный контракт для legacy Stop без
`_joiny_origin_adapter`.

Проверки: focused **26/26**, полный suite **305/305** за **343.244 s**,
contract + frozen complexity gates **PASS**. Baseline не пересчитывался.
