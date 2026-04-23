# analyzetg

Локальный Python CLI: скачивает историю Telegram-чатов (личные, группы,
форум-топики, каналы, комментарии), транскрибирует голосовые/кружочки/видео
и анализирует всё это через OpenAI. По умолчанию работает с
«непрочитанным» — точкой, на которой остановился твой Telegram-клиент —
и сохраняет результаты в файлы `reports/` с кликабельными ссылками на
исходные сообщения.

```bash
# 1. один раз: залогиниться в Telegram и проверить ключ OpenAI
uv run analyzetg init

# 2. без <ref> — интерактивный выбор чата, потом запуск
uv run analyzetg analyze          # выбрать чат → preset → период → analyze
uv run analyzetg dump             # выбрать чат → период → dump
uv run analyzetg describe         # выбрать чат → показать детали

# 3. с <ref> — прямой запуск без меню
uv run analyzetg analyze @somegroup
uv run analyzetg dump @somegroup -o unread.md
uv run analyzetg describe @somegroup

# 4. отрендерить результат прямо в терминале (без файла)
uv run analyzetg analyze @somegroup --console

# 5. пройтись по всем чатам с непрочитанным — в интерактивном режиме
#    первой строкой будет "🚀 Run on ALL N unread chats"
uv run analyzetg analyze
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

### Куда попадает результат

- По умолчанию `analyze` пишет файл `reports/{chat}-{preset}-{YYYY-MM-DD_HHMM}.md`
  и печатает в консоль только путь к нему. Никаких простыней markdown
  в терминал.
- `-o <path>` — свой путь.
- `--console` / `-c` — отрендерить результат прямо в терминале через
  Rich (нормальные заголовки, списки, таблицы, без сырого markdown).
  С `-o` вместе работает: покажет в терминале И сохранит в файл.
- В режиме без `<ref>` (обход всех непрочитанных) `-o` трактуется как
  директория: получается по файлу на чат.

### Ссылки на сообщения в результатах

Пресеты цитируют источник каждого тезиса / решения / задачи ссылкой
вида `[#12345](https://t.me/username/12345)`. Для приватных чатов —
`https://t.me/c/<internal>/12345`, для форум-топиков — с префиксом
`/{topic_id}/`. Работает для публичных каналов, супергрупп, форумов и
закрытых групп. Для личных переписок (нет username, chat_id > 0) ссылки
не формируются — пресет подставляет просто `#12345`.

## Типичные сценарии

```bash
# непрочитанное → summary-пресет (дефолт), файл в reports/
uv run analyzetg analyze @somegroup

# отрендерить в терминал красиво, без файла
uv run analyzetg analyze @somegroup --console

# последние 7 дней → дайджест в конкретный файл
uv run analyzetg analyze @somegroup --last-days 7 --preset digest -o out.md

# найти самое ценное, не читая всё
uv run analyzetg analyze @somegroup --preset highlights

# открытые вопросы, на которые стоит вернуться
uv run analyzetg analyze @somegroup --preset questions

# собрать внешние ссылки из чата
uv run analyzetg analyze @somegroup --preset links

# с конкретного сообщения (ссылка несёт msg_id)
uv run analyzetg analyze "https://t.me/somegroup/10000"

# вся история, action_items → в файл
uv run analyzetg analyze @somegroup --full-history --preset action_items

# проанализировал и сразу пометил прочитанным в TG
uv run analyzetg analyze @somegroup --mark-read

# только скачать историю (без OpenAI)
uv run analyzetg dump @somegroup -o history.md --last-days 30
uv run analyzetg dump @somegroup -o dump.jsonl --format jsonl --with-transcribe

# форум-топик — тред берём из ссылки или через --thread
uv run analyzetg analyze "https://t.me/somegroup/123" --last-days 3
uv run analyzetg analyze @somegroup --thread 123 --full-history
```

Без `<ref>` команда показывает таблицу всех диалогов с непрочитанным,
спрашивает подтверждение и гонит preset по каждому. По умолчанию пишет
в `reports/` по файлу на чат; `-o <dir>` — своя директория; `--console` —
отрендерить всё подряд прямо в терминал.

## Форумы (топики)

Форум-чаты (группы с топиками) обрабатываются отдельно — у каждого
топика свой маркер непрочитанного, и общий маркер диалога ничего не
значит. Есть три режима, которые работают и для `analyze`, и для `dump`:

```bash
# один конкретный топик — обычный анализ, t.me-ссылки содержат /thread/
uv run analyzetg analyze @forumchat --thread 42

# одно саммари по всему форуму (требует явный период)
uv run analyzetg analyze @forumchat --all-flat --last-days 3

# по отдельному файлу на каждый топик (unread по каждому)
uv run analyzetg analyze @forumchat --all-per-topic
# → reports/{chat-slug}/{topic-slug}-summary-YYYY-MM-DD_HHMM.md
```

Если запустить `analyze @forumchat` без флагов в терминале — покажется
таблица топиков и интерактивный выбор (номер топика / `A`ll-flat /
`P`er-topic / `Q`uit). В non-TTY режиме команда требует флаг явно.

`analyzetg describe @forumchat` показывает топики со счётчиками непрочитанного
и количеством сообщений, уже лежащих в локальной БД.

## Интерактивный режим

Вшит в `analyze`, `dump`, `describe` как дефолт: запускаешь команду без
`<ref>` → мастер. Передаёшь `<ref>` → прямой запуск без меню.

```bash
uv run analyzetg analyze            # → выбор чата (список с unread) → ...
uv run analyzetg analyze @chat      # → прямой запуск
```

Мастер для `analyze`: чат → (если форум) топик/режим → пресет → период
(unread / 7 дней / 30 дней / вся история / свои даты) → подтверждение.
Output и mark-read задаются флагами на команду (`--console`, `-o`,
`--mark-read`) — они показываются в шапке мастера.

Первая строка списка чатов — "🚀 Run on ALL N unread chats" — запускает
batch-обработку всех непрочитанных (сохраняет старое поведение).

## Ссылки на чат

`<ref>` принимает всё:

- `@username`
- `https://t.me/durov` / `https://t.me/durov/123` (с msg_id)
- `https://t.me/somegroup/100/5000` (форум-топик)
- `https://t.me/c/1234567890/5000` (приватная ссылка)
- `https://t.me/+AbCdEf...` (invite — добавь `--join`)
- `-1001234567890` — числовой chat_id. Все три варианта работают:
  `analyzetg analyze -1001234567890` (CLI автоматически экранирует
  минус), `analyzetg analyze -- -1001234567890`,
  `analyzetg analyze 1001234567890` (без минуса — тоже интерпретируется
  как канал `-100xxxxxxxxxx`).
- `"Bull Trading"` (fuzzy-поиск по диалогам)

## CLI cheatsheet

`analyzetg --help` разбивает команды на три группы:

**Main** — для ежедневного использования:

| Команда | Что делает |
|---|---|
| `init` | Авторизация Telegram + smoke-check OpenAI |
| `describe [<ref>]` | Без ref — интерактивный пик чата → детали. С ref — детали конкретного чата. С `--all`/`--kind`/`--search`/`--limit` — табличный обзор |
| `analyze [<ref>] [...]` | Без ref — мастер (чат → пресет → период → запуск). С ref — прямой запуск. Флаги `--console` / `-o` / `--mark-read` |
| `dump [<ref>] [...]` | Без ref — мастер (чат → период → запуск). С ref — прямой дамп в md/jsonl/csv |

**Sync & subscriptions** — долгосрочное слежение за набором чатов:

| Команда | Что делает |
|---|---|
| `sync [--chat] [--thread] [--dry-run]` | Инкрементально докачать новое по всем подпискам |
| `chats add/list/enable/disable/remove` | Управление подписками |
| `transcribe [--chat] [--since] [--limit]` | Транскрибировать voice/vnote/video |

**Maintenance** — обслуживание:

| Команда | Что делает |
|---|---|
| `stats [--since] [--by]` | Траты + cache hit rate |
| `cleanup --retention 90d` | NULL-ить старые тексты сообщений |
| `cache purge --older-than 30d` | Очистка кэша анализа |

Старые `dialogs`/`topics`/`channel-info`/`resolve`/`backfill`/`export`
остались как скрытые алиасы (не показываются в `--help`, но продолжают
работать), чтобы не ломать существующие скрипты. Их функциональность
вошла в `describe` / `analyze` / `dump`.

### Полезные флаги `analyze` / `dump`

- `--console` / `-c` — рендер в терминал вместо файла (Rich: заголовки,
  таблицы, цитаты).
- `--mark-read` — после обработки продвинуть Telegram-маркер прочитанного
  до последнего проанализированного сообщения (видно в других клиентах).
- `--full-history` / `--from-msg <id>` / `--last-days N` /
  `--since / --until` — задать период вручную, обойти unread-дефолт.
- `--thread <id>` / `--all-flat` / `--all-per-topic` — режимы для форум-чатов
  (см. раздел «Форумы»).
- `-o <path>` — свой путь для вывода (файл для одиночного ref,
  директория для no-ref и per-topic режимов).
- `--no-cache` — отключить локальный `analysis_cache` (принудительно
  пересчитать).

### Пресеты для `analyze`

Живут в [`presets/*.md`](presets/). Можно править, можно добавлять свои —
файл = пресет.

| Пресет | Что выдаёт |
|---|---|
| `summary` | Топ-3 темы + 5–10 тезисов + тон + ключевые сообщения (дефолт) |
| `digest` | Короткий пронумерованный список тем, 1–2 строки на каждую |
| `action_items` | Markdown-таблица задач: `Кто / Что / Срок / Статус / Ссылка` |
| `decisions` | Markdown-таблица решений: `Решение / Кто / Когда / Обоснование / Ссылка` |
| `highlights` | 5–15 самых ценных сообщений, отсортированных по важности |
| `questions` | Таблица открытых вопросов (`без ответа` / `частично` / `ответ был, но не консенсус`) |
| `quotes` | Дословные памятные цитаты с автором и ссылкой |
| `links` | Внешние URL из чата, сгруппированные по темам |
| `custom --prompt-file path.md` | Свой промпт одноразово, без файла в `presets/` |

Формат пресета и как добавить свой — в [`presets/README.md`](presets/README.md).

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

Выходные отчёты — в `reports/` (по умолчанию gitignored).

Кэш на трёх уровнях:

1. **Дедуп транскрипций по `document_id`** — один голос = одна транскрипция,
   даже если переслан в 10 чатов.
2. **Локальный `analysis_cache`** по `sha256(preset|version|model|sorted(msg_ids)|opts)`.
   Меняешь промпт в пресете → бампаешь `prompt_version` → кэш
   инвалидируется для этого пресета.
3. **OpenAI prompt caching** (автоматически при длине префикса > 1024 токенов;
   фиксированный порядок *system → static → dynamic* и `temperature=0.2`
   максимизируют хиты).

Map-reduce включается автоматически, когда период не влезает в один chunk:
дешёвая модель (`gpt-5.4-nano`) собирает mini-summaries по фрагментам,
умная (`gpt-5.4`) — сводит их в финальный отчёт. Каждый map-вызов
кэшируется независимо → досинк одного фрагмента пересчитывает только его.

Инкрементальная выгрузка: перед тем как идти в Telegram за сообщениями,
`analyze` / `dump` смотрят на максимальный `msg_id` в локальной БД над
порогом — если он выше маркера непрочитанного, дозапрашиваются только
ещё более новые сообщения. Повторные запуски без новых событий в чате
вообще не ходят в сеть.

## Разработка

```bash
uv run pytest              # unit-тесты
uv run ruff check .        # линт
uv run ruff format .       # форматирование
```

Полная спецификация: [`docs/analyzetg-spec.md`](docs/analyzetg-spec.md).

## Лицензия

MIT — см. [LICENSE](LICENSE).
