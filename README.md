# analyzetg

Локальный Python CLI для выгрузки сообщений из Telegram (личные чаты, группы,
форум-топики, каналы, комментарии к постам), транскрипции голосовых/кружочков/
видео и анализа всего этого через OpenAI API с агрессивным кэшированием.

Единый внешний вендор — OpenAI (`chat.completions` для анализа,
`audio.transcriptions` для Whisper / gpt-4o-transcribe). Всё остальное — локально
в SQLite.

## Требования

- Python ≥ 3.11
- [`uv`](https://github.com/astral-sh/uv)
- `ffmpeg` на PATH (нужен только для транскрипций видео/кружков; для voice —
  не обязателен)
- Telegram API credentials — `api_id`/`api_hash` с <https://my.telegram.org>
- OpenAI API key

## Установка

```bash
git clone https://github.com/maxbolgarin/analyzetg.git
cd analyzetg
uv sync --extra dev
cp .env.example .env
cp config.toml.example config.toml
chmod 700 storage                          # БД не шифруется — полагаемся на FS-права
```

Отредактируйте `.env`:

```
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=<hash>
OPENAI_API_KEY=sk-...
```

`config.toml` содержит разумные дефолты из §13 спеки — менять не обязательно.

## Quick start

```bash
# 1. Авторизация Telegram + проверка OpenAI key
uv run analyzetg init

# 2. Посмотреть свои чаты
uv run analyzetg dialogs --limit 30
uv run analyzetg dialogs --search "Trading" --kind supergroup

# 3. Диагностика ссылок
uv run analyzetg resolve "https://t.me/durov/123"

# 4. Подписаться и выкачать последние 7 дней (default_lookback_days)
uv run analyzetg chats add @somegroup
uv run analyzetg sync

# 5. Транскрибировать голосовые / кружочки
uv run analyzetg transcribe --chat -1001234567890 --limit 20

# 6. Получить саммари
uv run analyzetg analyze --chat -1001234567890 \
    --preset summary --last-days 7 --output out.md

# 7. Счётчики расходов
uv run analyzetg stats --by preset
```

Если нужна **только история в файл** без анализа через OpenAI — одна команда:

```bash
uv run analyzetg dump @somegroup -o history.md --last-days 30
uv run analyzetg dump @somegroup -o history.md --full-history --with-transcribe
uv run analyzetg dump "https://t.me/c/1234567890" -o dump.jsonl --format jsonl --no-subscribe
```

`dump` резолвит ссылку, синхронизирует сообщения (инкрементально — второй
запуск не скачивает то, что уже есть), и пишет md/jsonl/csv. По умолчанию
создаёт подписку — тогда обычный `sync --all` будет поддерживать историю
в актуальном состоянии. Флаг `--no-subscribe` выключает это поведение.

## CLI cheatsheet

| Команда | Что делает |
|---|---|
| `init` | Авторизация Telegram + smoke-check OpenAI |
| `dialogs [--search] [--kind] [--limit]` | Таблица всех твоих чатов |
| `topics <ref>` | Список топиков форум-группы |
| `resolve <anything>` | Диагностика: что это за ссылка/ID/fuzzy |
| `channel-info <ref>` | Подписчики канала, linked discussion |
| `chats add <ref> [...]` | Добавить подписку (см. опции ниже) |
| `chats list [--enabled-only]` | Список подписок |
| `chats enable/disable/remove <id>` | Включить/выключить/удалить |
| `sync [--chat] [--thread] [--dry-run]` | Инкрементальная выгрузка |
| `backfill --chat --from-msg` | Бэкфил истории назад/вперёд |
| `transcribe [--chat] [--since] [--limit]` | Транскрибировать voice/vnote/video |
| `analyze --chat --preset summary [...]` | Анализ через OpenAI |
| `stats [--since] [--by]` | Траты + cache hit rate |
| `cache purge --older-than 30d` | Очистка кэша анализа |
| `cleanup --retention 90d` | NULL-ить старые тексты сообщений |
| `export --chat --format md --output` | Экспорт уже выкачанных сообщений в md/jsonl/csv |
| `dump <ref> -o file.md [...]` | Одной командой: скачать историю и сохранить в файл (без OpenAI-анализа) |

### Пресеты для `analyze`

- `summary` — ключевые тезисы (5–10 пунктов)
- `action_items` — кто что должен сделать
- `digest` — короткий дайджест
- `decisions` — принятые решения
- `custom --prompt-file path.md` — свой промпт

### Чат-референсы (`<ref>`)

Принимается всё:

- `@username`
- `https://t.me/durov` / `https://t.me/durov/123`
- `https://t.me/somegroup/100/5000` (топик)
- `https://t.me/c/1234567890/5000` (приватная ссылка)
- `https://t.me/+AbCdEf...` (invite — добавьте `--join`)
- `-1001234567890` (числовой chat_id)
- `"Bull Trading"` (fuzzy-поиск по твоим чатам)

## Архитектура в двух словах

```
CLI (Typer)  ──►  Resolver (Telethon)     ──►  SQLite: chats, subscriptions
                  Sync (iter_messages)    ──►  SQLite: messages, sync_state
                  Transcriber (OpenAI)    ──►  SQLite: media_transcripts (дедуп по doc_id)
                  Analyzer (OpenAI)       ──►  SQLite: analysis_cache, analysis_runs, usage_log
```

Два отдельных SQLite файла в `storage/`:

- `session.sqlite` — сессия Telethon.
- `data.sqlite` — всё остальное: чаты, сообщения, транскрипты, кэш анализа,
  журнал токенов/цен.

Прокси-кэш на трёх уровнях:

1. Дедуп транскрипций по `document_id` — один голос = одна транскрипция, даже
   если переслан в 10 чатов.
2. Локальный `analysis_cache` по `sha256(preset|version|model|sorted(msg_ids)|opts)`.
3. OpenAI prompt caching (автоматически при длине префикса > 1024 токенов;
   фиксированный порядок *system → static → dynamic* и `temperature=0.2`
   максимизируют хиты).

Map-reduce включается автоматически, когда период не влезает в один chunk:
дешёвая модель собирает mini-summaries по фрагментам, умная — сводит их в
финальный отчёт. Каждый map-вызов кэшируется независимо → досинк одного
фрагмента пересчитывает только его.

## Разработка

```bash
uv run pytest              # 44 unit-теста (links, hasher, chunker, formatter, filters, resolver)
uv run ruff check .        # линт
uv run ruff format .       # форматирование
```

Полная спецификация: [`docs/analyzetg-spec.md`](docs/analyzetg-spec.md).

## Лицензия

MIT — см. [LICENSE](LICENSE).
