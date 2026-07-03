# Установка Joiny-Mnemonic для Claude Code

Практическая инструкция по подключению долговременной памяти к Claude Code.

## 1. Требования

- Python 3.11+
- pip

```powershell
python --version
python -m pip install -e .
```

Distribution называется `joiny-mnemonic`, Python-пакет — `joiny_mnemonic`.

## 2. Установка хуков для одного проекта

Выполнить в папке нужного проекта:

```powershell
joiny-mnemonic --project-root . install-hooks claude-code
```

Будет обновлён `.claude/settings.json`. Состояние проекта хранится в
`.joiny-mnemonic/memory.db`. Если уже существует старая `.llm-memory/memory.db`, она
используется на месте без разрушительной миграции.

Перед записью существующий `settings.json` проверяется. Невалидный JSON не изменяется, а
команда завершается с указанием строки и столбца ошибки. Валидный файл копируется в
`settings.json.joiny-mnemonic.bak`; после записи новый JSON проверяется, а при ошибке
восстанавливается исходная версия.

## 3. Глобальная установка

Один пользовательский hook можно установить для всех проектов:

```powershell
joiny-mnemonic install-hooks claude-code --global
```

Конфигурация записывается в `$CLAUDE_CONFIG_DIR/settings.json` или, если переменная не
задана, в `~/.claude/settings.json`. В установленной команде нет фиксированного пути к
проекту: при каждом вызове Joiny-Mnemonic определяет рабочую папку и поднимается до
ближайшего `.git`.

## 4. Что происходит во время сессии

- `SessionStart` внедряет компактный resume-пакет.
- `UserPromptSubmit` и `PostToolUse` сохраняются идемпотентно.
- Сырые токены этих событий считаются до редукции tool output.
- При достижении раннего порога создаётся снапшот и внедряется
  `[CONTEXT CHECKPOINT]`, до нативного сжатия Claude Code.
- Полный исходный tool output остаётся доступен через exact-source promotion.

## 5. Проверка

```powershell
joiny-mnemonic capabilities --agent claude-code
joiny-mnemonic timeline
joiny-mnemonic verify
```

В `memory_capabilities`/CLI нужно различать:

- `hook_installer_available=true` — установщик существует;
- `hooks_configured=true` — команда найдена в валидном project/global config;
- `hook_runtime_verified=true` — хотя бы один hook уже дошёл до этой базы.

MCP сам по себе не сохраняет обычный диалог или маркеры `Goal:/Decision:/Fact:`. При первом
MCP-подключении сервер явно предупреждает об этом и подсказывает `install-hooks`, если
автоматический capture не настроен.

Создать снапшот вручную:

```powershell
joiny-mnemonic snapshot
```

## 6. Обновление со старого имени

Повторный `install-hooks` заменяет сгенерированные команды `python -m llm_memory` на
`python -m joiny_mnemonic`, не дублируя hook delivery. Новые команды и плагины используют
имя Joiny-Mnemonic; старое имя сохраняется только для чтения существующей базы и legacy
plugin entry points.

## 7. Ограничения

- Репозиторные тесты проверяют формат конфигурации и hook runtime, но не запускают реальный
  бинарник Claude Code.
- База остаётся локальной для каждого проекта, даже при глобальной установке hook.