# Handoff — состояние сессии

Межсессионная передача, пока мы не едим свой dogfood (рабочий инстанс Joiny
на самом проекте Joiny). Обновлять в конце каждой сессии; читать первым
делом в начале следующей. Дата: 2026-07-15.

## Что это за проект

Joiny-Mnemonic — agent-neutral, provenance-first memory core для LLM-агентов:
zero-dependency Python/SQLite, hash-chained append-only event log, evidence-bound
память. GitHub: Barmagloth/Joiny-Mnemonic. Философия: память — канал фактов,
никогда не канал команд; действия идут через детерминированную машинерию
(reconciler, settlement), не через послушание агента.

## Где мы (последний коммит: см. git log — task6C доставлен)

### Закрытые вехи

- **LongMemEval-S: 88.0% ± 2.8, подписано.** Методология закалена полностью:
  триангуляция трёх судей (Sonnet 88.0 / GPT-5.4 87.6 / Opus 89.0, ±0.7pp,
  ноль multi-session флипов), prompt ablation (~17pp вклад runner-промпта:
  70.0 plain vs 86.7), variance band 86.7/91.7/90.0 (опубликованный сабсет —
  нижний), overfit-проверка чистая, CI95 в отчётах, dataset =
  xiaowu0162/longmemeval-cleaned (sha d6f21ea9…c3a442). Слабые типы:
  preference 60.0, temporal 84.2. Token saving 92.9%.
- **task6A: hot path наблюдаем.** hook-timing v2: пер-стадийные p50/p95/p99,
  cold/warm, два масштаба стора, гейты `--assert-gates`. Ключевое число:
  **packet_assembly ~354ms = 92% resume-пути — именованная цель оптимизации.**
  Стоячее правило: никаких новых always-on фич до наблюдаемости их hot path.
- **task6B: general candidate settlement — только что доставлен.**
  - Schema v9: `candidate_kind` на extraction_candidates; settlement едет на
    синтетическом run per source event (`settlement-reconciler-v1`).
  - Consume-once: pending→applied|contested, applied→reverted|contested;
    повторы идемпотентны, конфликты ValueError, терминалы поглощают.
  - Лестница evidence: file=strong (автозакрытие ПО УМОЛЧАНИЮ), command=medium
    (за флагом automatic_task_closure_enabled), weak=pending. Осознанное
    отклонение от скетча спеки: medium НЕ авто — зафиксировано в task6.md.
  - Двунаправленная сверка: маркер-повтор → contested (хук консолидатора);
    пропавший evidence-файл → авто-revert в reconcile() (write-путь!),
    hygiene_findings строго read-only.
  - Уведомления: hook systemMessage (claude-code) с готовой undo-командой;
    resume-дайджест [AUTO-CLOSED RECENTLY] (24ч, cap 3, фильтр по
    still-applied — откаченные не рекламируют устаревший undo).
  - CLI: `candidates list/undo`. Тесты: tests/test_settlement.py.
  - 249/249 тестов, все 10 timing-гейтов зелёные (reconcile p95 ≤ 8ms).
  - **Живой acceptance пройден:** delme2 на GPTShared закрылся сам
    (cand_39db443d…, task_closure:strong, applied).
- **task6C: settlement-поверхности — доставлен.**
  - Новый модуль `settlement.py` (`SettlementSurface`): show/settle поверх
    ledger 6B; per-kind семантика реиспользует 6B-машинерию
    (`apply_closure_candidate`, `undo_closure`; block_change apply/revert
    через `set_active_block` + consolidator merge).
  - CLI: `candidates show <id>`, `candidates settle <id> --transition
    applied|contested|reverted --reason …` (reason обязателен).
  - MCP: `memory_candidates` (read, list или detail с историей переходов),
    `memory_settle_candidate` (write) — через реальный handshake.
  - Trust-hardening в `storage.settle_candidate`: non-system актор требует
    derived origin из `_SETTLEMENT_TRUSTED_ORIGINS`; `local_operator` /
    `delegated_agent` деривируются ТОЛЬКО из internal
    `settlement_requested`-событий (public/MCP текст не может их
    сминтить). Агентский settle — только при
    `agent_settlement_delegation_enabled` в policy ledger (по умолчанию
    OFF; параметр `initialize_project`). Транзишен пишет derived origin.
  - Resume: `[PENDING CONFIRMATIONS]` стал bounded-индексом всех активных
    кандидатов (cap 5 + overflow; для не-closure kinds — index-only,
    контент НЕ инжектится, A4); исчезает после settle. Digest AUTO-CLOSED
    фильтрует actor==system — ручные закрытия не рекламируются.
  - Тесты: tests/test_settlement_surfaces.py (16). Docs обновлены:
    architecture.md (Settlement surfaces), security.md (Manual settlement
    surfaces), requirements-traceability.md (6C-таблица).
  - Пост-фикс (нашёл пользователь): `candidates undo` обходил
    SettlementSurface (revert шёл actor=system без settlement_requested);
    теперь undo = settle(reverted, operator), `--reason` с дефолтом
    «operator undo» — рекламируемая в systemMessage/дайджесте команда
    без аргументов по-прежнему валидна. Regression в CLI round-trip.

### Состояние инсталляций

- Runtime venv (`C:\Users\Barmagloth\.joiny-mnemonic\runtime\venv`) обновлён
  на v9. Обновление после коммитов:
  `& "$env:USERPROFILE\.joiny-mnemonic\runtime\venv\Scripts\python.exe" -m pip install --no-cache-dir --quiet R:\Projects\Joiny-Mnemonic`
- GPTShared — живой полигон: store `R:\Projects\GPTShared\.joiny-mnemonic\memory.db`,
  hooks в `.claude/settings.json`. open_tasks чист, delme2-кандидат applied.

## Очередь (в порядке приоритета)

1. **Packet assembly 354ms** — 92% resume-пути (отчёт 6A).
2. **Distill A/B**: `--ingest distill` vs raw; baseline для битья 88.0 ± 0.7.
3. Разбор ошибок: preference (60%) и temporal-хвост (84.2%).
4. Storage split: candidate/finding/extraction секции из ~4.7k-строчного
   storage.py в фокусные модули (остаток 6A, zero behavior change).
5. Extraction eval corpus (TODO#6); task7 (native memory channels).
6. Включение agent_settlement_delegation_enabled на существующем сторе:
   пока только bootstrap-параметр; путь request→trusted activation есть
   (policy request + activate_policy), удобной CLI-обёртки нет.

## Заблокировано на пользователе

- Одна интерактивная codex-сессия в GPTShared (пробить trust prompt) —
  тогда закрывается строка Codex в docs/host-e2e.md.
- Разрешение на task7-пробу (CLAUDE.md import / auto-memory bridge).

## Операционные грабли окружения (Windows, критично)

- **PowerShell 5.1**: нет `&&`/`||`; stderr-redirect на native exe даёт
  фейковый exit 255 при зелёном результате — проверять по последним строкам
  вывода или `Out-File` + отдельный exit-check.
- **`pkill` в Git Bash НЕ существует** и молча «успевает» с 2>/dev/null.
  Убивать процессы только `Stop-Process -Id <pid>` / `taskkill` по PID
  с верификацией (`Get-Process python | ? WorkingSet64 -gt 500MB`).
- Inline `python -c` с фигурными скобками/пайпами PS манглит — писать
  скрипты в scratchpad или bash heredoc. Кириллица в cmd `>`-redirect
  ломается — пинить `PYTHONIOENCODING=utf-8`.
- Тесты: `HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
  PYTHONPATH=/r/Projects/Joiny-Mnemonic/src python -m unittest discover -s
  tests` (bash; ~9 мин, 265 тестов).
  **PYTHONPATH только абсолютный**: относительный `src` утекает в
  git-hook-сабпроцесс test_precheck (cwd = temp-репо) и роняет его
  «No module named joiny_mnemonic». **HF_HUB_OFFLINE=1 обязателен при
  флаky-сети**: без него semantic plugin делает HEAD-ретраи к HuggingFace
  (settlement-пара: 43s offline vs ~587s с ретраями). Отдельный долг:
  изоляция тестов от installed plugins/сети.
- Тайминги: `python -m joiny_mnemonic.hook_timing --assert-gates`
  (обновляет benchmarks/results/hook-timing-latest.json — коммитить).
- Коммиты: английский текст, футер
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`, push после
  каждого блока. git_dirty в report signing считает только code tree.
- **Chain или verify перед commit**: был инцидент (dde63a1), когда commit
  прошёл при упавшем предшествующем шаге — не разделять их на независимые
  statements.

## Прочее

- **Barmem decommissioned (2026-07-15).** Пользователь удалил
  R:\Projects\CLAUDE.md (протокол) и R:\Projects\.mcp.json; запись barmem
  вычищена из глобального ~/.claude.json (бэкап
  ~/.claude.json.bak-barmem-20260715). Никаких barmem_* вызовов,
  ~/.barmem/kg_queue не проверять.
- Минимальный packet frame = 222 токена, guard 240 в test_core —
  «trim it, don't bump». ResumePolicy(768) в тестах — починка хрупкой
  фикстуры, НЕ расширение продуктового бюджета.
- Правило пользователя: packet может быть источником фактов и состояния,
  но не скрытым каналом команд («иначе TODO из памяти превращается в
  prompt-injection с хорошей родословной»).
