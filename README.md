# analyzetg

Локальный Python CLI: скачивает историю Telegram-чатов (личные, группы,
форум-топики, каналы, комментарии), транскрибирует голосовые/кружочки/видео
и анализирует всё это через OpenAI. По умолчанию работает с «непрочитанным»
— точкой, на которой остановился твой Telegram-клиент.

```bash
# 1. один раз: залогиниться в Telegram и проверить ключ OpenAI
uv run analyzetg init

# 2. проанализировать непрочитанное в одном чате
uv run analyzetg analyze @somegroup

# 3. просто сохранить непрочитанное в файл, без OpenAI
uv run analyzetg dump @somegroup -o unread.md

# 4. пройтись по всем чатам с непрочитанным сразу (спросит подтверждение)
uv run analyzetg analyze -o ./reports
```

Никаких подписок, никакого «сперва sync, потом analyze» — одна команда
резолвит ссылку, дочитывает только то, что ты ещё не видел, и гонит
результат через OpenAI. Всё остальное — локально в SQLite.

## Требования

- Python ≥ 3.11, [`uv`](https://github.com/astral-sh/uv)
- `ffmpeg` на PATH — только для транскрипции видео/кружков
- Telegram `api_id`/`api_hash` с <https://my.telegram.org>
- OpenAI API key

## Установка

```bash
git clone https://github.com/maxbolgarin/analyzetg.git
cd analyzetg
uv sync --extra dev
cp .env.example .env        # впиши TELEGRAM_API_ID / _HASH / OPENAI_API_KEY
cp config.toml.example config.toml
chmod 700 storage           # БД не шифруется — полагаемся на FS-права
```

После этого `uv run analyzetg init` — интерактивный логин в Telegram.

## Что делает команда по умолчанию

`analyze <ref>` и `dump <ref>` по умолчанию берут **только непрочитанные**
сообщения: у Telegram-диалога есть маркер `read_inbox_max_id` — все
сообщения с `id > marker` считаются непрочитанными. Мы их выкачиваем и
анализируем.

Порядок приоритета флагов начала (первый совпавший выигрывает):

1. `--full-history` — вся история чата.
2. `--from-msg <id>` (или ссылка на сообщение) — начиная с этого
   сообщения включительно.
3. `--since YYYY-MM-DD` / `--until YYYY-MM-DD` / `--last-days N` — по дате.
4. без флагов → непрочитанное.

Если флагов нет и непрочитанного тоже нет — команда сразу выходит и
подсказывает, как проанализировать что-то другое.

## Типичные сценарии

```bash
# непрочитанное → summary-пресет (дефолт)
uv run analyzetg analyze @somegroup

# последние 7 дней → дайджест в файл
uv run analyzetg analyze @somegroup --last-days 7 --preset digest -o out.md

# с конкретного сообщения
uv run analyzetg analyze "https://t.me/somegroup/10000"

# вся история, action_items
uv run analyzetg analyze @somegroup --full-history --preset action_items

# только скачать историю (без OpenAI)
uv run analyzetg dump @somegroup -o history.md --last-days 30
uv run analyzetg dump @somegroup -o dump.jsonl --format jsonl --with-transcribe

# форум-топик — тред берём из ссылки или через --thread
uv run analyzetg analyze "https://t.me/somegroup/123" --last-days 3
uv run analyzetg analyze @somegroup --thread 123 --full-history
```

Без `<ref>` команда показывает таблицу всех диалогов с непрочитанным,
спрашивает подтверждение и гонит preset по каждому. `-o <dir>` сохранит
один файл на чат (`{chat_id}-{slug}.md`), без `-o` просто напечатает в
консоль.

## Ссылки на чат

`<ref>` принимает всё:

- `@username`
- `https://t.me/durov` / `https://t.me/durov/123` (с msg_id)
- `https://t.me/somegroup/100/5000` (форум-топик)
- `https://t.me/c/1234567890/5000` (приватная ссылка)
- `https://t.me/+AbCdEf...` (invite — добавь `--join`)
- `-1001234567890` (числовой chat_id — обязательно через `--`:
  `analyzetg analyze -- -1001234567890`)
- `"Bull Trading"` (fuzzy-поиск по диалогам)

## CLI cheatsheet

| Команда | Что делает |
|---|---|
| `init` | Авторизация Telegram + smoke-check OpenAI |
| `dialogs [--search] [--kind] [--limit]` | Таблица твоих чатов с unread-счётчиками |
| `topics <ref>` | Список топиков форум-группы |
| `resolve <anything>` | Диагностика: как парсится ссылка/ID/fuzzy |
| `channel-info <ref>` | Подписчики канала, linked discussion |
| `analyze [<ref>] [...]` | Анализ чата (дефолт — непрочитанное) |
| `dump [<ref>] -o file [...]` | Скачать историю в md/jsonl/csv без OpenAI |
| `transcribe [--chat] [--since] [--limit]` | Транскрибировать voice/vnote/video |
| `stats [--since] [--by]` | Траты + cache hit rate |
| `cache purge --older-than 30d` | Очистка кэша анализа |
| `cleanup --retention 90d` | NULL-ить старые тексты сообщений |
| `export --chat --format md --output` | Экспорт уже выкачанных сообщений |

### Пресеты для `analyze`

- `summary` — ключевые тезисы (5–10 пунктов, дефолт)
- `action_items` — кто что должен сделать
- `digest` — короткий дайджест
- `decisions` — принятые решения
- `custom --prompt-file path.md` — свой промпт

### Когда нужны подписки (`chats add` / `sync`)

Они для долгосрочного слежения за набором чатов: подписываешься,
`sync --all` по крону докачивает новое, ты можешь накопить историю
и потом гонять `analyze` по датам, не теряя контекст между запусками.

Для одноразового «глянь, что там нового» это не нужно — `analyze <ref>`
сам резолвит чат и докачивает недостающее.

```bash
uv run analyzetg chats add @somegroup          # подписаться
uv run analyzetg chats list                    # что подписано
uv run analyzetg sync                          # докачать новое по всем
uv run analyzetg chats remove <chat_id>        # отписаться
```

## Архитектура в двух словах

```
CLI (Typer)  ──►  Resolver (Telethon)     ──►  SQLite: chats
                  backfill (iter_messages)──►  SQLite: messages
                  Transcriber (OpenAI)    ──►  SQLite: media_transcripts
                  Analyzer (OpenAI)       ──►  SQLite: analysis_cache, runs, usage_log
```

Два SQLite файла в `storage/`:

- `session.sqlite` — сессия Telethon.
- `data.sqlite` — чаты, сообщения, транскрипты, кэш анализа, журнал токенов/цен.

Кэш на трёх уровнях:

1. Дедуп транскрипций по `document_id` — один голос = одна транскрипция,
   даже если переслан в 10 чатов.
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
uv run pytest              # unit-тесты
uv run ruff check .        # линт
uv run ruff format .       # форматирование
```

Полная спецификация: [`docs/analyzetg-spec.md`](docs/analyzetg-spec.md).

## Лицензия

MIT — см. [LICENSE](LICENSE).
