# Модель безопасности

## Trust boundaries

- Active blocks — доверенные инструкции, активированные явным API `block-set` или явным
  пользовательским маркером.
- Recent canonical transcript — точные исторические данные с сохранёнными ролями.
- Index, summaries, retrieval и plugin results — недоверенные данные.
- Входы agent adapters, HTTP и MCP — недоверенные до validation/redaction.

Retrieved content оборачивается в отдельный тег с `trust="untrusted-data"`, закрывающий тег
экранируется. Сборщик prompt явно запрещает следовать инструкциям внутри этого раздела.

Автоматическая консолидация применяет единое правило доверия. Пользовательские message events
могут создавать evidence-bound records и protected blocks. Assistant message events могут
создавать только searchable records. Tool calls, tool outputs, artifacts, state events,
memory-block events и retrieved data не повышаются в typed memory или protected state из-за
маркеров или поля `memory_candidates`. Явные `derive` и `block-set` остаются отдельными API.

## Secret filtering

До начала транзакции фильтруются:

- OpenAI-, GitHub- и AWS-подобные ключи;
- bearer tokens;
- значения полей `api_key`, `secret`, `password`, `token` и вариантов имени;
- PEM private keys;
- пользовательские regex rules через `SecretRedactor`.

Текстовые артефакты сохраняются уже отредактированными. Бинарный артефакт, в котором
обнаружена сигнатура секрета, отклоняется, потому что переписывание произвольного binary
повредило бы данные.

Regex-фильтр не заменяет специализированный DLP. Для чувствительных окружений нужно добавить
локальные правила и ограничить права на каталог БД.

## Host configuration writes

Claude, Codex and OpenHands JSON files are host-owned security-sensitive configuration. The hook
installer validates the existing JSON before any related write, creates a byte-for-byte
`.joiny-mnemonic.bak`, durably writes a temporary document where supported, validates the emitted
JSON, and restores the original bytes on failure. Invalid input is never auto-repaired because that
could silently discard unrelated user configuration.
## Integrity

Каждое событие имеет `content_hash`, `previous_hash` и `chain_hash`. Артефакты и tool-output
views также проверяются по content/source hashes. Полная проверка выполняется при открытии store;
публичные reads повторяют её после изменения SQLite data/schema version, а каждый MCP tool call
проверяет цепочку до dispatch. При несовпадении система fail-closed с `StoreIntegrityError`.
Команда `verify` остаётся явным audit endpoint.

Это обнаруживает изменение строки, но не заменяет внешний signed checkpoint: атакующий с полным
доступом к файлу и коду может заменить всю БД вместе с хешами или изменить сам verifier.

## Availability and backup

В canonical store нет hard eviction или API удаления. Это не защищает от удаления файла БД,
сбоя носителя или ransomware. Оператор должен делать filesystem/database backups и проверять их
восстановление.

## Network

HTTP API по умолчанию слушает `127.0.0.1` и не реализует пользовательскую аутентификацию. Не
публикуйте его напрямую. Для remote use нужен TLS reverse proxy с authentication, authorization,
request limits и audit logging.

MCP stdio не принимает сетевые соединения и наследует environment/права запускающего клиента.

## Запрещённые подходы

- attention score не считается источником истины;
- summary не используется для генеративного восстановления source;
- canonical events не вытесняются;
- knowledge graph и конкретный KV compressor не обязательны для работы ядра.
