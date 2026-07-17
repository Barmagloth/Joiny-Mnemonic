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
- **Плагины в runtime venv (2026-07-16):** semantic-local + reranker-local
  установлены — живая инсталляция теперь = конфигурация опубликованных
  88.0%. Цена измерена на месте: полная свежепроцессная доставка
  205ms → 275ms (+70ms на entry-points; страшные ~800ms из 6A cold-probe
  не воспроизводятся на тёплой машине). Первый семантический запрос по
  непустому индексу разово грузит модель (секунды; в бюджете
  cold_first_resume). После обновлений core не забывать, что плагины
  ставятся отдельно: `pip install R:\Projects\Joiny-Mnemonic\plugins\semantic-local
  R:\Projects\Joiny-Mnemonic\plugins\reranker-local`.

### Packet assembly 354ms — РЕШЕНО (2026-07-15)

354ms → **41-49ms p50 / ≤55ms p95** (resume-доставка целиком p95 ≤74ms,
было ~450). Три причины, три структурных фикса (детали в
docs/performance.md):
1. Witness-реестр: глобальный witnesses.json вырос до 1.5MB/1920 записей
   (813 бенчмарки + 537 тесты — эфемерные ран-ы), каждая доставка
   читала+переписывала его целиком дважды. Теперь пер-проектные шарды
   `witnesses.d/<project_id>.json` (монолит — read-only fallback для
   миграции), env `JOINY_MNEMONIC_WITNESS_REGISTRY` для изоляции,
   hook_timing изолирует реестр per-run. Тест-двойник MemoryWitness
   переехал на шов `_read_project`/`_write_project`.
2. Fingerprint на каждый resume хешировал ВСЕ файлы проекта + git
   сабпроцесс. Теперь `file_hash_cache` (rebuildable проекция в SQLite,
   ключ size+mtime_ns — та же эвристика, что у git index) + чтение
   `.git/HEAD`/refs напрямую.
3. Тройная материализация снапшота (latest→restore→tail) схлопнута в
   одну; verify full-blob — хеш декомпрессированных байтов вместо
   канонической ре-сериализации.
Остаточный пол: decompress+parse снапшота ~18ms + stat-свип ~13ms.
Бюджеты 6A не трогал (loose tripwires). Полный сьют стал быстрее на ~4
минуты (322s против 560s) — тот же witness-фикс.

### Distill A/B — РЕШЕНО (2026-07-16, benchmarks/results/distill-ab.md)

Вердикт: **внутри band** (ожидаемые полные 500 ≈ 87.4), с типовым
перераспределением. Парные пробы против строк подписанного raw-рана:
- stratified 60: 52/60 у обоих плеч (одинаково);
- preference (все 30): 60.0 → 66.7 (+3/−1) — дистиллированные факты дают
  синтез вкусовой эвиденции; n=30, judge-sensitive, сигнал направления;
- knowledge-update (все 78): 96.2 → 89.7 (0/−5) — **stale-fact poisoning**:
  уверенный датированный факт-атом со СТАРЫМ значением перевешивает
  поздний апдейт (все 5 провалов — ровно эта форма, покрытие gold ~100%,
  т.е. не retrieval).
Решение: flat-дистилляция без supersession не получает default-on;
TODO#6 наследует заострённую цель — update-aware distillation
(супersede/valid_to для противоречащих фактов; машинерия в ledger уже
есть). Полный 500-ран сознательно НЕ гонялся (~сутки квоты, решение не
меняет). Инфраструктура: benchmarks/prewarm_distill.py — параллельный
прогрев контентно-адресуемого distill-кеша (6573 сессии, 0 фейлов);
кеш в benchmarks/distill-cache/ (gitignored) — переиспользуется, если
полный ран всё же понадобится (останется ~12.7k сессий догреть).

### Связность/evidence-set — обсуждение закрыто суженным вердиктом (2026-07-17)

Измеренное основание: смешение статусов (derived выжимка подавляла raw
апдейт, KU 96.2→89.7); независимый top-k плохо комплектует контекст
(session-diversity packing существует не зря); наивная diversity тоже
вредит (breadth-first 0/11); опциональные каналы умирают молча (живой
инстанс неделю на lexical-only). Это доказывает необходимость
РАЗЛИЧАТЬ raw/derived/current/superseded — и НЕ доказывает
необходимость графа, ECO-онтологии или универсального evidence-слоя.

Оправданы только четыре узких направления (формулировка пользователя):
1. ~~Health/watermark каналов~~ — **СДЕЛАН (2026-07-17)**: проекция
   retrieval_channel_health, каждая ветка помечает успех
   (watermark=head на момент sync) или ошибку с таймстампом; дельты
   персистятся (минутная грануляция успехов). Потребители:
   capabilities.core.retrieval_health (lag, degraded, absent_optional —
   явный сигнал «semantic не установлен», наш инцидент) + bounded
   staleness-строки в resume. Стадия hook-timing retrieval_health warm
   p95 = 0.31ms, все гейты зелёные, 274 теста. tests/test_retrieval_health.py.
2. **Не терять существующий статус источника** (raw/derived/superseded/
   quarantined) там, где это меняет ranking/packing или отображение
   provenance. Новую онтологию не создавать.
3. **Любой новый set-selector** сравнивать не с абстрактным top-k, а с
   настроенным session-diversity packing, на замороженном наборе.
   **Census v2 (2026-07-17, после ревью — v1 переврал «90% reader»)**:
   трёхступенчатый воспроизводимый split всех 60 провалов raw-500
   (census.md + census-deep-latest.json с пулами и текстами фрагментов):
   retrieval 1, session-packing 5, **passage:no 17** (сессии
   представлены, но нужного фрагмента в пакете НЕТ — passage-selection
   внутри покрытых сессий, в осн. multi-session 10 + temporal 5),
   **passage:yes 11** (доказанные reader-провалы — ответ строкой в
   пакете), indeterminate 26 (неэкстрактивные: preference 12,
   temporal 8). Итого: селекторный потолок НЕ ~1pp, а до ~4.6pp —
   passage-level selection вернулся в игру; доказанный reader-класс —
   18%, не 90%. Гейт графа закрыт по-прежнему (класс связности не
   измерен). Прокси — строковое вхождение ответа, оговорки в обе
   стороны записаны в census.md.
4. **Граф — только после** появления набора вопросов, где все нужные
   фрагменты найдены, а ответ провален из-за отсутствия связывающего
   пути. Такого набора и подтверждённого класса ошибок сейчас нет.

## Очередь (в порядке приоритета)

1. **Update-aware ось решена keyed-выжимкой (2026-07-16, stage 3 в
   distill-ab.md): KU 89.7 → 93.6** (raw 96.2). Форма SCD/Graphiti:
   дистиллятор (уже оплаченный вызов, LME_DISTILL_KEYED=1, отдельный
   кеш benchmarks/distill-cache-keyed, gitignored) выдаёт факт+ключ
   `субъект|атрибут`; закрытие по ключу детерминированное
   (`--ingest distill-keyed`). Декомпозиция контролем без закрытия
   (92.3): промпт-инструкция «явно фиксируй новое значение» чинит 3/5
   провалов сама (recall), закрытие добавляет чистый value-update класс
   + одно воздержание, цена — 1 кейс. Подтверждённая ловушка:
   закрытие прячет конкретный факт за расплывчатым преемником с тем же
   ключом (gym 7:00 PM → «recurring commitments») — продуктовый вывод:
   valid_to-ограничение вместо жёсткой supersession и/или требовать
   значение у преемника. Токен-перекрытие (distill-aware) — измеренный
   тупик (88.5, 170 ложных на 2 настоящих). Остаток до raw: отравленное
   воздержание (вне досягаемости update-механики) + шум.
   Preference-проверка сделана (2026-07-17): keyed 21/30 = 70.0 против
   flat 66.7 / raw 60.0 — выигрыш вкусов сохранён. Ячейка keyed полная:
   бьёт flat по обеим осям; наивная экстраполяция на 500 ≈ 88.2 =
   паритет с raw внутри band. До шипа: extraction-корпус (гейт 0.9/0.7)
   и продуктовый дизайн valid_to-ограничения вместо жёсткой
   supersession.
1b. ~~[CHECK MATERIAL]~~ — **закрыт как accuracy-эксперимент по
   пре-регистрированному правилу (2026-07-17,
   benchmarks/results/check-material.md)**: на решающей abstention-30
   пробе воспроизводимых механизменных флипов нет (v1-флип вверх не
   воспроизвёлся в v2, все флипы вниз — с cm_tokens=0, т.е. шум);
   guard на вкусах пройден после фикса резерва (23/30 против 21);
   60-скан ровный (52=52), скрытых регрессий keyed-формы нет
   (temporal 10/10, ms 7/10=flat=raw). Целевой кейс 031748ae секция
   ДОСТИГЛА (конфликт 4-vs-5 предъявлен, модель о нём рассуждает), но
   его настоящая ловушка — несовпадение должности, вне досягаемости.
   Цена: срабатывание 3-43% вопросов, ~220 токенов, build p95 <5ms.
   Найден и исправлен паттерн: секция обязана стоить НОЛЬ эвиденции,
   когда пуста (v1-резерв 600 токенов съел весь preference-регресс).
   Решение пользователя (2026-07-17): флаг НЕ продуктизировать — никакого
   API/схемы вокруг него, код живёт только в benchmark harness; в бэклог
   (может вернёмся после полной отладки, может нет). Verify/auditability
   НЕ смешивать с этим отрицательным результатом: CHECK MATERIAL пытался
   улучшить ответ модели, verify-поверхность помогает человеку проверять
   источники — разные задачи, разные метрики. Правило «пустая секция
   стоит ноль» возведено в общий инвариант packet assembly:
   docs/architecture.md + tests/test_packet_invariants.py (3 теста:
   продуктовый resume без пустого скаффолдинга, исчезновение секции
   после settle, harness-инвариант байт-в-байт).
2. Разбор ошибок: preference (60%) и temporal-хвост (84.2%) — с учётом
   distill-находок (preference частично закрывается фактами).
3. Storage split: candidate/finding/extraction секции из ~4.7k-строчного
   storage.py в фокусные модули (остаток 6A, zero behavior change).
4. Extraction: **ПЕРЕКВАЛИФИЦИРОВАНО ревью 2026-07-17 — это был research
   probe bridge-экстрактора (Haiku), НЕ продукт-гейт.** Поставляемый
   бэкенд (nuextract-local) не измерен вовсе; «gate passed» в ранних
   формулировках завышал идентичность эксперимента (испытуемого выбрал
   по удобству — claude CLI был готов, nuextract требовал установки —
   а слово поехало за артефактом). Продукт-гейт = тот же корпус против
   nuextract-local, в очереди TODO#6; может провалиться — это и есть
   смысл гейта. Первый цикл пробы (выводы сужены ревью): Доказано: механизм гейта останавливает
   плохую версию (ран-1 честно упал: preference en 1.00/0.78,
   ru 0.97/0.67 — все 17 ru-промахов = пустые выдачи бытовых вкусов);
   type-span-скоринг работает как регрессионный инструмент; три
   заявленные инъекционные зоны выдержали (знаменатель false_trusted —
   6 trap-примеров, НЕ 140; извлечённое из ловушек ушло в карантин).
   НЕ доказано: обобщение — промпт bridge-v2 исправлен по ошибкам
   этого же корпуса и пересужен на нём же → корпус стал development
   set, его числа en 0.98/1.00 ru 0.96/1.00 — dev-числа; качество
   не-preference типов (n=7/язык); широкая injection-устойчивость
   (6 ловушек = smoke); стабильность прогонов/версий Haiku.
   До любого включения: held-out транш, написанный ПОСЛЕ заморозки
   промпта, + повторные прогоны. Состав корпуса: 50 позитивов + 10
   негативов + 3 ловушки + 7 other-типов = 70/язык.
   **Независимый пересуд (GPT-5.4 via codex, read-only):**
   `pass_with_narrowed_claim`,
   benchmarks/results/extraction-codex-audit-20260717/ — подтвердил
   preference-числа и рамку 0-из-6-ловушек; добавил контрпримеры:
   EN fact precision 0.25 (1TP/3FP), exact-triple около нуля. Часть
   fact-FP — строки с реальным фактом, не внесённым в однотипное золото
   (pref-005, pref-030) → held-out делать с мульти-типным золотом.
   task7 (native memory channels).
4b. Option matrix (TODO#1): таблица «качество × цена» по вариантам
   retrieval-опций — lexical-only / +semantic / +reranker / +both — на
   stratified-60, парно против raw-строк; hook-timing уже даёт цену
   per-mode. ~4 bench-рана по ~1.3ч.
5. Включение agent_settlement_delegation_enabled на существующем сторе:
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
