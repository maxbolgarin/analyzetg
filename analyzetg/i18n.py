"""User-visible string lookups, keyed by language.

Covers every label that appears in saved reports, the wizard, ask
prompts/answers, and CLI banners. The LLM-facing prompt bodies live in
`presets/<language>/*.md` (loaded by `analyzer/prompts.py`) — those are
NOT in this table.

Lookup falls back to English when an entry is missing for the requested
language, so adding a third language is purely additive: drop new keys
where you have translations, leave others to fall through.

Resolution: `t(key)` (no `lang`) reads `settings.locale.language` lazily
at call time so test monkeypatches and `--language` overrides take
effect without any cached state.
"""

from __future__ import annotations

# Schema: key → {language_code → text}.
# English is the canonical baseline. Add language entries as you translate
# more strings; missing entries silently fall back to English.
_STRINGS: dict[str, dict[str, str]] = {
    # ---- Formatter preamble (saved reports + LLM prompt) ----
    "period_label": {"en": "Period", "ru": "Период"},
    "chat_label": {"en": "Chat", "ru": "Чат"},
    "messages_label": {"en": "Messages", "ru": "Сообщений"},
    "messages_in_group": {"en": "Messages in this group", "ru": "Сообщений в этой группе"},
    "topic_label": {"en": "Topic", "ru": "Топик"},
    "no_topic": {"en": "No topic", "ru": "Без топика"},
    "forum_label": {"en": "Forum", "ru": "Форум"},
    "topics_word": {"en": "topic(s)", "ru": "топик(ов)"},
    "and_more": {"en": "and {n} more", "ru": "…и ещё {n}"},
    "no_transcript": {"en": "no transcript", "ru": "без транскрипта"},
    "msg_link_label": {"en": "Message link", "ru": "Ссылка на сообщение"},
    "fragment_label": {"en": "Fragment", "ru": "Фрагмент"},
    "fragment_count_label": {"en": "Fragments", "ru": "Число фрагментов"},
    "link_label": {"en": "link", "ru": "ссылка"},
    # ---- Citation / verification headings ----
    "sources_heading": {"en": "Sources", "ru": "Источники"},
    "verification_heading": {"en": "Verification", "ru": "Проверка"},
    "truncation_warning": {
        "en": "**⚠ Output truncated** — the model hit its output limit.",
        "ru": "**⚠ Вывод обрезан** — модель упёрлась в лимит длины ответа.",
    },
    # ---- Ask user-template labels (LLM prompt + UI) ----
    "ask_question": {"en": "Question", "ru": "Вопрос"},
    "ask_context": {"en": "Context", "ru": "Контекст"},
    "ask_msgs_short": {"en": "msgs", "ru": "сообщ."},
    "ask_from_scope": {"en": "from", "ru": "из"},
    "ask_answer_with_citations": {
        "en": "Answer (with citations):",
        "ru": "Ответ (с цитатами):",
    },
    # ---- Wizard / interactive UI ----
    "wiz_back": {"en": "← Back", "ru": "← Назад"},
    "wiz_include_comments_q": {
        "en": "This channel has linked discussion comments. Include them in the analysis?",
        "ru": "Канал имеет привязанные комментарии. Включить их в анализ?",
    },
    "wiz_yes_with_comments": {
        "en": "Yes — channel + comments",
        "ru": "Да — канал + комментарии",
    },
    "wiz_no_only_posts": {
        "en": "No — channel posts only",
        "ru": "Нет — только посты канала",
    },
    # Confirm step (Run / Back / Cancel)
    "wiz_run_it_q": {"en": "Run it?", "ru": "Запустить?"},
    "wiz_run_choice": {"en": "Run", "ru": "Запустить"},
    "wiz_cancel_choice": {"en": "Cancel", "ru": "Отмена"},
    # Mark-read step
    "wiz_mark_read_q": {
        "en": "Mark the processed messages as read?",
        "ru": "Пометить обработанные сообщения как прочитанные?",
    },
    "wiz_mark_read_yes": {
        "en": "Yes — advance Telegram's read marker after analysis",
        "ru": "Да — сдвинуть метку прочтения в Telegram после анализа",
    },
    "wiz_mark_read_no": {
        "en": "No — keep messages unread in Telegram",
        "ru": "Нет — оставить сообщения непрочитанными в Telegram",
    },
    # Period step
    "wiz_pick_period": {"en": "Pick a period", "ru": "Выберите период"},
    "wiz_period_unread": {
        "en": "Unread (default) — since Telegram read marker",
        "ru": "Непрочитанные (по умолчанию) — с метки прочтения Telegram",
    },
    "wiz_period_last7": {"en": "Last 7 days", "ru": "Последние 7 дней"},
    "wiz_period_last30": {"en": "Last 30 days", "ru": "Последние 30 дней"},
    "wiz_period_full": {"en": "Full history", "ru": "Вся история"},
    "wiz_period_from_msg": {
        "en": "From a specific message (link or id)…",
        "ru": "С конкретного сообщения (ссылка или id)…",
    },
    "wiz_period_custom": {
        "en": "Custom date range…",
        "ru": "Свой диапазон дат…",
    },
    "wiz_period_n_msgs": {"en": "{n} msgs", "ru": "{n} сообщ."},
    # Optional model-side hint (only emitted when content_language differs
    # from active language). Stays in the active language so the model
    # parses the hint reliably.
    "content_language_hint": {
        "en": "The chat content is predominantly in {language}.",
        "ru": "Содержимое чата преимущественно на языке: {language}.",
    },
    # ---- Top-level CLI help (Typer panel headers + command one-liners) ----
    # Read at cli.py module-import time, after `_bootstrap_db_overrides`
    # has applied DB-saved locale settings to the singleton. Adding new
    # commands → add a one-liner here for both langs.
    "cli_app_help": {
        "en": "Pull Telegram chats, enrich media (voice/images/docs/links), and analyze via OpenAI — all local.",
        "ru": "Загружай Telegram-чаты, обогащай медиа (голос/изображения/доки/ссылки) и анализируй через OpenAI — всё локально.",
    },
    "cli_panel_main": {"en": "Main", "ru": "Основные"},
    "cli_panel_sync": {"en": "Sync & subscriptions", "ru": "Синхронизация и подписки"},
    "cli_panel_maint": {"en": "Maintenance", "ru": "Обслуживание"},
    # Command one-liners (Typer reads from each command's docstring; we
    # set `help=` explicitly so the i18n lookup wins). Keys mirror the
    # function name where unambiguous.
    "cmd_init": {
        "en": "Interactive first-time setup: log in to Telegram and verify OpenAI key.",
        "ru": "Интерактивная первичная настройка: вход в Telegram и проверка ключа OpenAI.",
    },
    "cmd_describe": {
        "en": "List chats (no ref) or inspect one chat (with ref).",
        "ru": "Список чатов (без ref) или подробности одного чата (с ref).",
    },
    "cmd_analyze": {
        "en": "Analyze a chat. Default window = messages since your Telegram read marker.",
        "ru": "Анализ чата. По умолчанию — сообщения после маркера прочтения Telegram.",
    },
    "cmd_ask": {
        "en": "Answer a question about your synced Telegram archive.",
        "ru": "Ответ на вопрос по синхронизированному архиву Telegram.",
    },
    "cmd_dump": {
        "en": "Dump chat history to a file. Default window = messages since your Telegram read marker.",
        "ru": "Сохранение истории чата в файл. По умолчанию — сообщения после маркера прочтения.",
    },
    "cmd_sync": {
        "en": "Incrementally fetch new messages for all (or one) subscriptions.",
        "ru": "Инкрементально подтянуть новые сообщения для всех (или одной) подписок.",
    },
    "cmd_chats": {
        "en": "Manage subscriptions (what to sync).",
        "ru": "Управление подписками (что синхронизировать).",
    },
    "cmd_folders": {
        "en": "List your Telegram folders (for use with `analyze --folder NAME` / `dump --folder NAME`).",
        "ru": "Список папок Telegram (для `analyze --folder NAME` / `dump --folder NAME`).",
    },
    "cmd_stats": {
        "en": "Aggregate API spend, cache hit rate and run counts.",
        "ru": "Сводка расходов на API, hit-rate кэша и число запусков.",
    },
    "cmd_cleanup": {
        "en": "Null-out old message texts; keep transcripts/analysis cache.",
        "ru": "Очистить старые тексты сообщений; транскрипты и кэш анализа сохраняются.",
    },
    "cmd_watch": {
        "en": "Run an inner `atg` command on a fixed cadence.",
        "ru": "Периодически запускать внутреннюю команду `atg`.",
    },
    "cmd_doctor": {
        "en": "Preflight check: Telegram session, OpenAI key, ffmpeg, DB integrity, presets, disk.",
        "ru": "Проверка готовности: сессия Telegram, ключ OpenAI, ffmpeg, целостность БД, пресеты, диск.",
    },
    "cmd_backup": {
        "en": "Snapshot storage/data.sqlite to a single compact file (uses VACUUM INTO).",
        "ru": "Сделать снимок storage/data.sqlite в один компактный файл (через VACUUM INTO).",
    },
    "cmd_restore": {
        "en": "Replace storage/data.sqlite with a backup. The current DB is moved aside.",
        "ru": "Заменить storage/data.sqlite резервной копией. Текущая БД отодвигается в сторону.",
    },
    "cmd_cache": {"en": "Analysis cache maintenance.", "ru": "Обслуживание кэша анализа."},
    "cmd_settings": {
        "en": "Manage persistent user settings (DB-backed overrides).",
        "ru": "Управление сохранёнными настройками (переопределения в БД).",
    },
    "cmd_reports": {"en": "Manage saved reports/", "ru": "Управление сохранёнными отчётами reports/"},
    # ---- Repeated phrases ------------------------------------------------
    "cancelled": {"en": "Cancelled.", "ru": "Отменено."},
    "aborted": {"en": "Aborted.", "ru": "Прервано."},
    "done": {"en": "Done.", "ru": "Готово."},
    "ok": {"en": "OK", "ru": "ОК"},
    "saved": {"en": "Saved", "ru": "Сохранено"},
    "yes": {"en": "Yes", "ru": "Да"},
    "no": {"en": "No", "ru": "Нет"},
    "retry": {"en": "Try again", "ru": "Попробуйте ещё раз"},
    "not_a_valid_choice": {
        "en": "Not a valid choice. Try again.",
        "ru": "Недопустимый выбор. Попробуйте ещё раз.",
    },
    "aborting_yes_set": {
        "en": "Aborting (--yes set, no confirmation possible).",
        "ru": "Прерываем (установлен --yes, подтверждение невозможно).",
    },
    # ---- Resolve / fetch progress ---------------------------------------
    "resolving": {"en": "→ Resolving {ref}", "ru": "→ Определяем {ref}"},
    "resolved_chat": {
        "en": "→ Resolved {label} (id={chat_id}, kind={kind}{thread_part})",
        "ru": "→ Распознано {label} (id={chat_id}, тип={kind}{thread_part})",
    },
    "fetching_new_messages": {
        "en": "→ Fetching new messages from Telegram...",
        "ru": "→ Загружаем новые сообщения из Telegram...",
    },
    "reading_unread_marker": {
        "en": "→ Reading unread marker...",
        "ru": "→ Читаем маркер непрочитанных...",
    },
    "unread_after_marker": {
        "en": "→ {n} unread message(s) after msg_id={marker}",
        "ru": "→ {n} непрочитанных сообщений после msg_id={marker}",
    },
    "have_up_to_local": {
        "en": "→ Have up to msg_id={msg_id} locally, fetching only newer",
        "ru": "→ Локально есть до msg_id={msg_id}, загружаем только новее",
    },
    "have_from_local": {
        "en": "→ Have from msg_id={msg_id} locally, fetching older history…",
        "ru": "→ Локально есть от msg_id={msg_id}, загружаем более старую историю…",
    },
    "no_local_msgs_full_history": {
        "en": "→ No local messages; fetching full chat history…",
        "ru": "→ Локальных сообщений нет; загружаем всю историю чата…",
    },
    "summary_pass": {"en": "→ {summary}", "ru": "→ {summary}"},
    "select_topics_for_flat": {
        "en": "Select topics for flat-forum analysis (or press Q to quit)...",
        "ru": "Выберите топики для flat-forum анализа (или нажмите Q для выхода)...",
    },
    "running_chat_unread_marker": {
        "en": "▶ {label} ({n} unread)",
        "ru": "▶ {label} ({n} непрочитанных)",
    },
    "per_topic_unread_unsupported": {
        "en": (
            "Per-topic unread isn't exposed by Telegram for arbitrary threads.\n"
            "Pass --last-days N, --from-msg <id>, or --full-history."
        ),
        "ru": (
            "Telegram не отдаёт маркер непрочитанных по произвольным топикам.\n"
            "Передайте --last-days N, --from-msg <id> или --full-history."
        ),
    },
    "no_unread_in_chat": {
        "en": "No unread messages in chat {chat_id}. Pass --last-days / --from-msg / --full-history to analyze anyway.",
        "ru": "В чате {chat_id} нет непрочитанных. Передайте --last-days / --from-msg / --full-history, чтобы анализировать всё равно.",
    },
    "no_unread_topics": {
        "en": "No topics with unread messages. Pass --last-days / --full-history to analyze everything anyway.",
        "ru": "Нет топиков с непрочитанными. Передайте --last-days / --full-history, чтобы анализировать все.",
    },
    "topic_header_with_unread": {
        "en": ">> {title} (topic_id={tid}, {n} unread)",
        "ru": ">> {title} (topic_id={tid}, {n} непрочитанных)",
    },
    "topic_header_no_unread": {
        "en": ">> {title} (topic_id={tid})",
        "ru": ">> {title} (topic_id={tid})",
    },
    "chat_header": {
        "en": ">> {title} ({n} unread)",
        "ru": ">> {title} ({n} непрочитанных)",
    },
    "folder_rule_based_unsupported": {
        "en": "Folder '{title}' uses category rules (contacts/groups/bots/etc.) without explicit chats — rule expansion isn't supported.",
        "ru": "Папка '{title}' использует правила-категории (контакты/группы/боты и т.п.) без явных чатов — расширение правил не поддерживается.",
    },
    "including_comments": {
        "en": "→ Including comments from linked chat {title} ({linked_id}) for window {since} … {until}",
        "ru": "→ Включаем комментарии из связанного чата {title} ({linked_id}) за окно {since} … {until}",
    },
    "stale_read_marker": {
        "en": "→ Stale read marker (msg_id={marker}, latest={latest}, unread={unread}); trusting unread badge → start at msg_id={start}",
        "ru": "→ Устаревший маркер прочтения (msg_id={marker}, последнее={latest}, непрочит.={unread}); доверяем счётчику → начинаем с msg_id={start}",
    },
    "using_chat_from_msg_link": {
        "en": "→ Using chat from --msg link; ignoring ref '{ref}'",
        "ru": "→ Используем чат из --msg-ссылки; игнорируем ref '{ref}'",
    },
    "repeating_last_run": {
        "en": "→ Repeating last run from {ts}",
        "ru": "→ Повторяем прошлый запуск от {ts}",
    },
    "no_unread_across_chats": {
        "en": "No unread messages across the selected chats.",
        "ru": "Нет непрочитанных сообщений в выбранных чатах.",
    },
    "also_saved_to": {"en": "Also saved: {path}", "ru": "Также сохранено: {path}"},
    "written_to": {"en": "Written: {path}", "ru": "Записано: {path}"},
    "posted_to_n_msgs": {
        "en": "Posted to {label} ({n} message(s)).",
        "ru": "Опубликовано в {label} ({n} сообщ.).",
    },
    "stats_total_cost": {"en": "Total cost: ${cost:.4f}", "ru": "Итоговая стоимость: ${cost:.4f}"},
    "stats_hit_rate": {"en": "Cache hit rate: {rate:.1%}", "ru": "Hit-rate кэша: {rate:.1%}"},
    "forum_pick_prompt": {
        "en": "Pick topic id, A=all-flat, P=per-topic, Q=quit",
        "ru": "Выберите id топика, A=весь форум, P=по топикам, Q=выйти",
    },
    "mark_chat_msgs_read_q": {
        "en": "Mark this chat's messages as read?",
        "ru": "Отметить сообщения этого чата прочитанными?",
    },
    "mark_chats_read_after_dump_q": {
        "en": "Mark messages as read in Telegram after each chat is dumped?",
        "ru": "Отмечать сообщения прочитанными в Telegram после каждого чата (для dump)?",
    },
    "mark_chats_read_after_analyze_q": {
        "en": "Mark messages as read in Telegram after each chat is analyzed?",
        "ru": "Отмечать сообщения прочитанными в Telegram после каждого чата (для analyze)?",
    },
    # ---- Settings panel -----------------------------------------------
    "settings_banner": {
        "en": "atg settings — interactive editor.",
        "ru": "atg settings — интерактивный редактор.",
    },
    "settings_banner_hint": {
        "en": "↑/↓ navigate, Enter open, ESC or 'Done' to exit.",
        "ru": "↑/↓ навигация, Enter — открыть, ESC или «Готово» — выход.",
    },
    "settings_pick_prompt": {
        "en": "Pick a setting (or an action) — type to filter, ESC to exit",
        "ru": "Выберите настройку (или действие) — введите текст для фильтра, ESC — выход",
    },
    "settings_show_row": {
        "en": "📋  Show effective settings (printable table)",
        "ru": "📋  Показать действующие настройки (табличный вид)",
    },
    "settings_reset_row": {
        "en": "♻️   Reset all overrides (revert to config / defaults)",
        "ru": "♻️   Сбросить все переопределения (вернуться к config / по умолчанию)",
    },
    "settings_done_row": {"en": "✓  Done", "ru": "✓  Готово"},
    "settings_subaction_removed": {
        "en": "→ The `{action}` sub-command was removed. Opening the interactive editor instead — every action lives there now (Show / Reset are rows in the menu).",
        "ru": "→ Подкоманда `{action}` удалена. Открываем интерактивный редактор — все действия теперь там (Показать / Сбросить — строки в меню).",
    },
    "settings_no_questionary": {
        "en": "questionary not available — falling back to non-interactive show.",
        "ru": "questionary недоступен — показываем неинтерактивный список.",
    },
    "settings_done_with_changes": {
        "en": "Done. Changes apply on the next `atg` invocation. (In-process settings refreshed.)",
        "ru": "Готово. Изменения применятся при следующем запуске `atg`. (Настройки в памяти обновлены.)",
    },
    "settings_done_no_changes": {"en": "Done. No changes saved.", "ru": "Готово. Изменений не сохранено."},
    "settings_nothing_to_reset": {
        "en": "Nothing to reset — no overrides saved.",
        "ru": "Сбрасывать нечего — переопределений не сохранено.",
    },
    "settings_cleared_n": {"en": "Cleared {n} override(s).", "ru": "Очищено переопределений: {n}."},
    "settings_cleared_key": {
        "en": "  → cleared {key} (now follows config / default)",
        "ru": "  → сброшено {key} (теперь следует config / значение по умолчанию)",
    },
    "settings_saved_kv": {
        "en": "  → saved {key}={value}",
        "ru": "  → сохранено {key}={value}",
    },
    "settings_empty_value": {"en": "(empty)", "ru": "(пусто)"},
    "settings_drop_n_q": {
        "en": "Drop {n} saved setting(s)? This cannot be undone.",
        "ru": "Удалить сохранённых настроек: {n}? Действие необратимо.",
    },
    "settings_no_pricing_models": {
        "en": "No models in pricing table — pick a custom name below.",
        "ru": "Нет моделей в таблице цен — выберите «своё имя» ниже.",
    },
    "settings_custom_model_row": {
        "en": "Custom… (type a model name)",
        "ru": "Своё… (введите имя модели)",
    },
    "settings_keep_current": {"en": "(keep current)", "ru": "(оставить как есть)"},
    "settings_exit_row": {"en": "✕ Exit settings", "ru": "✕ Выйти из настроек"},
    "settings_clear_follow_ui": {
        "en": "(clear / follow UI language)",
        "ru": "(сбросить / следовать UI-языку)",
    },
    "settings_clear_autodetect": {"en": "(autodetect)", "ru": "(автоопределение)"},
    "settings_state_on": {"en": "On", "ru": "Вкл"},
    "settings_state_off": {"en": "Off", "ru": "Выкл"},
    "settings_value_on": {"en": "on", "ru": "вкл"},
    "settings_value_off": {"en": "off", "ru": "выкл"},
    "settings_value_unset": {"en": "(unset)", "ru": "(не задано)"},
    "settings_value_follows_ui": {"en": "(follows UI)", "ru": "(следует UI)"},
    "settings_value_autodetect": {"en": "(autodetect)", "ru": "(автоопределение)"},
    "settings_int_prompt": {
        "en": "{label} [{desc}] — current: {current}. New value (blank = keep, 'q' = exit)",
        "ru": "{label} [{desc}] — текущее: {current}. Новое значение (пусто — оставить, 'q' — выход)",
    },
    "settings_not_an_integer": {
        "en": "Not an integer: {raw}. Try again or blank to cancel.",
        "ru": "Не целое число: {raw}. Попробуйте ещё раз или пусто — отмена.",
    },
    "settings_must_be_nonneg": {
        "en": "Must be ≥ 0. Try again or blank to cancel.",
        "ru": "Должно быть ≥ 0. Попробуйте ещё раз или пусто — отмена.",
    },
    "settings_custom_model_prompt": {
        "en": "Model name for {key} (blank to keep current)",
        "ru": "Имя модели для {key} (пусто — оставить текущее)",
    },
    "settings_show_table_title": {
        "en": "atg settings — effective values",
        "ru": "atg settings — действующие значения",
    },
    "settings_table_col_category": {"en": "category", "ru": "категория"},
    "settings_table_col_key": {"en": "key", "ru": "ключ"},
    "settings_table_col_effective": {"en": "effective", "ru": "действующее"},
    "settings_table_col_override": {"en": "override (DB)", "ru": "переопределение (БД)"},
    "settings_no_overrides_yet": {
        "en": "No DB overrides yet — values come from config.toml / defaults.",
        "ru": "Переопределений в БД пока нет — значения берутся из config.toml / по умолчанию.",
    },
    # Setting categories
    "settings_cat_languages": {"en": "Languages", "ru": "Языки"},
    "settings_cat_models": {"en": "Models", "ru": "Модели"},
    "settings_cat_enrich": {"en": "Enrichment defaults", "ru": "Обогащение (по умолчанию)"},
    "settings_cat_analyze": {"en": "Analyze tuning", "ru": "Настройка анализа"},
    # Setting labels
    "set_label_locale_language": {"en": "UI language", "ru": "Язык интерфейса"},
    "set_label_locale_content_language": {
        "en": "Content / LLM-input language",
        "ru": "Язык контента / промптов LLM",
    },
    "set_label_audio_language": {
        "en": "Whisper transcription hint",
        "ru": "Подсказка языка для Whisper",
    },
    "set_label_chat_model": {
        "en": "Analyze / ask flagship model",
        "ru": "Флагманская модель для analyze / ask",
    },
    "set_label_filter_model": {"en": "Filter / cheap model", "ru": "Дешёвая / фильтр-модель"},
    "set_label_audio_model": {"en": "Audio transcription model", "ru": "Модель транскрибации аудио"},
    "set_label_vision_model": {"en": "Vision (image) model", "ru": "Vision-модель (изображения)"},
    "set_label_voice": {"en": "Voice → transcript", "ru": "Голос → транскрипт"},
    "set_label_videonote": {"en": "Video-note → transcript", "ru": "Видеосообщение → транскрипт"},
    "set_label_video": {"en": "Video → transcript", "ru": "Видео → транскрипт"},
    "set_label_image": {"en": "Image → vision description", "ru": "Изображение → vision-описание"},
    "set_label_doc": {"en": "Doc → text extract", "ru": "Документ → извлечение текста"},
    "set_label_link": {"en": "Link → page summary", "ru": "Ссылка → саммари страницы"},
    "set_label_high_impact": {
        "en": "High-impact reactions threshold",
        "ru": "Порог высокореакционных сообщений",
    },
    "set_label_dedupe_forwards": {
        "en": "De-duplicate identical forwards",
        "ru": "Объединять одинаковые пересылки",
    },
    "set_label_min_msg_chars": {
        "en": "Minimum message length (chars)",
        "ru": "Минимальная длина сообщения (символы)",
    },
    # Setting descriptions
    "set_desc_locale_language": {
        "en": "Wizard, report headings, formatter labels.",
        "ru": "Визард, заголовки отчётов, метки форматтера.",
    },
    "set_desc_locale_content_language": {
        "en": "Picks `presets/<lang>/` tree. Empty = follow UI language.",
        "ru": "Выбирает дерево `presets/<lang>/`. Пусто = следовать UI-языку.",
    },
    "set_desc_audio_language": {
        "en": "Empty = autodetect. Decoupled from UI / content language.",
        "ru": "Пусто = автоопределение. Не зависит от UI / контентного языка.",
    },
    "set_desc_chat_model": {
        "en": "Reduce-phase + single-pass + ask answer model.",
        "ru": "Reduce-фаза + одно-проход + ответ ask.",
    },
    "set_desc_filter_model": {
        "en": "Map-phase + rerank + self-check + link/doc enrichers.",
        "ru": "Map-фаза + rerank + self-check + обогатители ссылок/документов.",
    },
    "set_desc_audio_model": {
        "en": "Used by voice / videonote / video enrichers.",
        "ru": "Используется обогатителями voice / videonote / video.",
    },
    "set_desc_vision_model": {
        "en": "Used when image enrichment is enabled.",
        "ru": "Используется при включённом обогащении изображений.",
    },
    "set_desc_enrich_default": {
        "en": "Default for `--enrich`.",
        "ru": "Значение по умолчанию для `--enrich`.",
    },
    "set_desc_high_impact": {
        "en": "Marker `[high-impact]` added when sum(reactions) ≥ N. 0 disables.",
        "ru": "Маркер `[high-impact]` ставится при sum(reactions) ≥ N. 0 — отключено.",
    },
    "set_desc_dedupe_forwards": {
        "en": "Collapses repeated forwards into a single `[×N]`-marked entry.",
        "ru": "Объединяет повторяющиеся пересылки в одну запись с `[×N]`.",
    },
    "set_desc_min_msg_chars": {
        "en": "Drop messages whose effective body is shorter than N chars.",
        "ru": "Отбрасывать сообщения короче N символов в эффективном теле.",
    },
    # ---- Wizard banner / tips ------------------------------------------
    "wiz_banner": {
        "en": "analyzetg — interactive mode  (action: {action})",
        "ru": "analyzetg — интерактивный режим  (действие: {action})",
    },
    "wiz_tips": {
        "en": "Tips: type letters to filter (e.g. uni → UNION), ↑/↓ navigate, Enter select, ESC back, Ctrl-C cancel.",
        "ru": "Подсказки: введите буквы для фильтра (напр. uni → UNION), ↑/↓ — навигация, Enter — выбор, ESC — назад, Ctrl-C — отмена.",
    },
    "wiz_pick_chat_n_unread": {
        "en": "Pick a chat — {n} with unread, sorted by count (type to filter, ↑/↓ to move)",
        "ru": "Выберите чат — {n} с непрочитанными, отсортировано по количеству (введите для фильтра, ↑/↓ — перемещение)",
    },
    "fetching_message": {
        "en": "→ Fetching message {msg_id} from Telegram...",
        "ru": "→ Загружаем сообщение {msg_id} из Telegram...",
    },
    "listing_forum_topics": {
        "en": "→ Listing forum topics...",
        "ru": "→ Получаем список топиков форума...",
    },
    "listing_forum_topics_for_flat": {
        "en": "→ Listing forum topics for flat-forum grouping...",
        "ru": "→ Получаем список топиков для группировки flat-forum...",
    },
    "looking_up_topic_marker": {
        "en": "→ Looking up topic's unread marker...",
        "ru": "→ Получаем маркер непрочитанных для топика...",
    },
    "running_analysis": {"en": "→ Running analysis...", "ru": "→ Запускаем анализ..."},
    "filtered_per_topic": {
        "en": "→ Filtered per-topic: kept {kept} / dropped {dropped}",
        "ru": "→ Фильтрация по топикам: оставлено {kept} / отброшено {dropped}",
    },
    "no_unread_in_topic": {
        "en": "No unread messages in topic '{title}'.",
        "ru": "В топике '{title}' нет непрочитанных сообщений.",
    },
    "topic_unread_count": {
        "en": "→ {n} unread in '{title}' after msg_id={marker}",
        "ru": "→ {n} непрочитанных в '{title}' после msg_id={marker}",
    },
    "topic_not_found": {
        "en": "Topic {thread_id} not found in this forum.",
        "ru": "Топик {thread_id} не найден в этом форуме.",
    },
    "no_topics_in_forum": {"en": "No topics in this forum.", "ru": "В этом форуме нет топиков."},
    "no_topic_with_id": {
        "en": "No topic with id={tid}.",
        "ru": "Топик с id={tid} не найден.",
    },
    "no_unread_topics_after_refresh": {
        "en": "No unread forum topics after refresh.",
        "ru": "После обновления нет непрочитанных топиков.",
    },
    "no_dialogs_with_unread": {
        "en": "No dialogs with unread messages.",
        "ru": "Нет диалогов с непрочитанными сообщениями.",
    },
    "no_chats_in_folder_unread": {
        "en": "No chats in this folder have unread messages.",
        "ru": "В этой папке нет чатов с непрочитанными.",
    },
    "no_folder_matching": {
        "en": "No folder matching '{folder}'. Available folders: {titles}",
        "ru": "Нет папки '{folder}'. Доступные папки: {titles}",
    },
    "folder_unread_chats": {
        "en": "→ Folder {title} — {n}/{total} unread chats match",
        "ru": "→ Папка {title} — совпало {n}/{total} непрочитанных чатов",
    },
    "channel_no_linked": {
        "en": "→ Channel has no linked discussion group; skipping comments.",
        "ru": "→ У канала нет связанной группы обсуждений; пропускаем комментарии.",
    },
    "fetching_comments": {
        "en": "→ Fetching {n} comment(s)...",
        "ru": "→ Загружаем комментариев: {n}...",
    },
    "no_comments_in_window": {
        "en": "→ No comments in window.",
        "ru": "→ В заданном окне нет комментариев.",
    },
    "marked_read_topics": {
        "en": "→ Marked read across {marked}/{total} topics",
        "ru": "→ Отмечено прочитанным в {marked}/{total} топиках",
    },
    "marked_read_up_to": {
        "en": "→ Marked read up to msg_id={msg_id}",
        "ru": "→ Отмечено прочитанным до msg_id={msg_id}",
    },
    "couldnt_mark_read": {
        "en": "⚠ Could not mark as read: {err}",
        "ru": "⚠ Не удалось отметить прочитанным: {err}",
    },
    "couldnt_post_to": {
        "en": "⚠ Could not post to {target}: {err}",
        "ru": "⚠ Не удалось отправить в {target}: {err}",
    },
    # ---- Cost / budget --------------------------------------------------
    "estimated_cost_band": {
        "en": "Estimated cost (analysis only): ${lo:.4f}–${hi:.4f}",
        "ru": "Оценка стоимости (только анализ): ${lo:.4f}–${hi:.4f}",
    },
    "estimate_unavailable": {
        "en": "Cost estimate unavailable — pricing missing for one of the run's models.",
        "ru": "Оценка стоимости недоступна — нет цены для одной из моделей.",
    },
    "estimate_enrich_note": {
        "en": "Note: enrichment cost (voice/image/video/doc/link) is NOT included.",
        "ru": "Примечание: стоимость обогащения (voice/image/video/doc/link) НЕ включена.",
    },
    "max_cost_exceeded": {
        "en": "⚠ Estimated cost ${lo:.4f}–${hi:.4f} exceeds --max-cost ${max:.4f} ({n} messages, preset={preset}).",
        "ru": "⚠ Оценка ${lo:.4f}–${hi:.4f} превышает --max-cost ${max:.4f} ({n} сообщ., пресет={preset}).",
    },
    "max_cost_not_enforced": {
        "en": "→ --max-cost not enforced: pricing missing for one of the run's models.",
        "ru": "→ --max-cost не применяется: нет цены для одной из моделей.",
    },
    "run_anyway_q": {"en": "Run anyway?", "ru": "Запустить всё равно?"},
    # ---- Single-message analyze ----------------------------------------
    "msg_not_found_in_chat": {
        "en": "Message {msg_id} not found in chat {chat_id}.",
        "ru": "Сообщение {msg_id} не найдено в чате {chat_id}.",
    },
    "failed_persist_msg": {
        "en": "Failed to persist message {msg_id}.",
        "ru": "Не удалось сохранить сообщение {msg_id}.",
    },
    "nothing_to_analyze_for_msg": {
        "en": "Nothing to analyze for msg {msg_id}.{hint}",
        "ru": "Нечего анализировать в сообщении {msg_id}.{hint}",
    },
    # ---- Dry run --------------------------------------------------------
    "dry_run_summary": {
        "en": "Dry run — would run preset {preset} over {n} message(s) on {final} (filter: {fil}).",
        "ru": "Dry run — пресет {preset} на {n} сообщ., модель {final} (filter: {fil}).",
    },
    "dry_run_unloadable": {
        "en": "Dry run — {n} message(s); preset {preset} not loadable.",
        "ru": "Dry run — {n} сообщ.; пресет {preset} не загружается.",
    },
    # ---- Per-topic / batch dispatch -------------------------------------
    "topic_failed": {"en": "Topic {title} failed: {err}", "ru": "Топик {title} не удался: {err}"},
    "batch_chat_failed": {"en": "Failed: {err}", "ru": "Не удалось: {err}"},
    "batch_complete_ok": {
        "en": "Batch complete: {ok}/{total} chats succeeded.",
        "ru": "Пакет завершён: успешно {ok}/{total} чатов.",
    },
    "batch_complete_with_failures": {
        "en": "Batch complete: {ok}/{total} chats succeeded, {fail} failed.",
        "ru": "Пакет завершён: успешно {ok}/{total}, не удалось {fail}.",
    },
    "process_chats_q": {
        "en": "Process {n} chat(s) with {total} total unread message(s)?",
        "ru": "Обработать {n} чат(ов), всего непрочитанных: {total}?",
    },
    "process_topics_q": {
        "en": "Process {n} topic(s){extra}?",
        "ru": "Обработать {n} топик(ов){extra}?",
    },
    "process_topics_with_unread": {
        "en": " with {total} unread",
        "ru": " ({total} непрочитанных)",
    },
    # ---- Wizard / interactive ------------------------------------------
    "wiz_pick_chat_to_describe": {
        "en": "analyzetg — pick a chat to describe",
        "ru": "analyzetg — выберите чат для описания",
    },
    "wiz_zero_messages_period": {
        "en": "0 messages in this period — nothing to analyze.",
        "ru": "0 сообщений за период — анализировать нечего.",
    },
    "wiz_no_unread_showing_all": {
        "en": "No chats with unread messages. Showing all dialogs.",
        "ru": "Нет чатов с непрочитанными. Показываем все диалоги.",
    },
    "wiz_no_dialogs_at_all": {"en": "No dialogs at all.", "ru": "Диалогов нет вовсе."},
    "wiz_pick_chat_n": {
        "en": "Pick a chat from {n} dialogs (type to filter, ↑/↓ to move)",
        "ru": "Выберите чат из {n} диалогов (введите для фильтра, ↑/↓ для навигации)",
    },
    # ---- Doctor ---------------------------------------------------------
    "doctor_missing": {"en": "Missing: {missing}.", "ru": "Отсутствует: {missing}."},
    "doctor_env_seen_at": {"en": "Checked .env at: {path}", "ru": "Проверен .env в: {path}"},
    "doctor_env_seen": {"en": "env seen: {seen}", "ru": "переменные окружения: {seen}"},
    "doctor_session_authorized": {
        "en": "Telegram session already authorized.",
        "ru": "Сессия Telegram уже авторизована.",
    },
    "doctor_logged_in": {"en": "Logged in.", "ru": "Авторизация выполнена."},
    "doctor_check_openai": {
        "en": "Checking OpenAI API key ...",
        "ru": "Проверяем ключ OpenAI ...",
    },
    "doctor_openai_ok": {"en": "OpenAI key OK.", "ru": "Ключ OpenAI работает."},
    "doctor_openai_failed": {
        "en": "OpenAI check failed: {err}",
        "ru": "Проверка OpenAI не удалась: {err}",
    },
    "doctor_summary_failed": {
        "en": "{fails} failure(s), {warns} warning(s).",
        "ru": "Ошибок: {fails}, предупреждений: {warns}.",
    },
    "doctor_summary_warned": {
        "en": "{warns} warning(s). Some features may be limited.",
        "ru": "Предупреждений: {warns}. Возможны ограничения функций.",
    },
    "doctor_all_ok": {"en": "All checks passed.", "ru": "Все проверки пройдены."},
    "init_phone_prompt": {
        "en": "Phone number (international, e.g. +491711234567)",
        "ru": "Номер телефона (в международном формате, напр. +491711234567)",
    },
    "init_login_code_prompt": {
        "en": "Login code from Telegram",
        "ru": "Код входа из Telegram",
    },
    "init_2fa_prompt": {"en": "2FA password", "ru": "Пароль двухфакторной аутентификации"},
    # ---- Ask / Q&A -----------------------------------------------------
    "ask_empty_question": {"en": "Empty question.", "ru": "Пустой вопрос."},
    "ask_continue_q": {
        "en": "Continue chatting?",
        "ru": "Продолжить диалог?",
    },
    "ask_no_keywords": {
        "en": "No useful keywords in your question. Add a noun, name, or topic — stop words and short tokens are filtered. (Or pass --semantic, which doesn't need keyword tokens.)",
        "ru": "В вопросе нет полезных ключевых слов. Добавьте существительное, имя или тему — стоп-слова и короткие токены отфильтровываются. (Или передайте --semantic — он не требует ключевых слов.)",
    },
    "ask_indexed_n": {
        "en": "Indexed {n} new message(s).",
        "ru": "Проиндексировано новых сообщений: {n}.",
    },
    "ask_index_up_to_date": {
        "en": "Nothing new to index — already up to date.",
        "ru": "Нечего индексировать — уже актуально.",
    },
    "ask_no_matching_messages": {
        "en": "No matching messages. Try `atg sync <chat>` first if the chat hasn't been backfilled, or broaden your scope.",
        "ru": "Нет подходящих сообщений. Попробуйте сначала `atg sync <chat>`, если чат не синхронизирован, или расширьте область поиска.",
    },
    "ask_no_matches_reusing": {
        "en": "→ No new matches; reusing prior context.",
        "ru": "→ Новых совпадений нет; используем предыдущий контекст.",
    },
    "ask_model_empty": {"en": "Model returned empty answer.", "ru": "Модель вернула пустой ответ."},
    "ask_refresh_failed": {
        "en": "⚠ {n} chat(s) failed to refresh; falling back to local data.",
        "ru": "⚠ Не удалось обновить {n} чат(ов); используем локальные данные.",
    },
    "ask_refreshed_total": {
        "en": "Refreshed: {total} new message(s) across {n} chat(s).",
        "ru": "Обновлено: {total} новых сообщений в {n} чатах.",
    },
    "ask_refreshed_none": {"en": "Refreshed: no new messages.", "ru": "Обновлено: новых сообщений нет."},
    "ask_refreshing": {
        "en": "→ Refreshing {n} chat(s) from Telegram...",
        "ru": "→ Обновляем чатов из Telegram: {n}...",
    },
    # ---- File output ---------------------------------------------------
    "saved_to_path": {"en": "Saved {path}", "ru": "Сохранено: {path}"},
    "wrote_msgs_to": {
        "en": "Wrote {n} message(s) to {path}",
        "ru": "Записано {n} сообщ. в {path}",
    },
    "also_saved": {"en": "Also saved: {path}", "ru": "Также сохранено: {path}"},
    "exported_n_to": {
        "en": "Exported {n} message(s) to {path}",
        "ru": "Экспортировано {n} сообщ. в {path}",
    },
    "unknown_format": {"en": "Unknown format: {fmt}", "ru": "Неизвестный формат: {fmt}"},
    "output_is_file_need_dir": {
        "en": "--output {path} is a single file; per-topic mode needs a directory.",
        "ru": "--output {path} это файл; для режима по топикам нужна директория.",
    },
    # ---- Truncation banner (saved report markdown) ---------------------
    "truncation_banner_md": {
        "en": (
            "> ⚠️ **Output was truncated.** The model hit "
            "`output_budget_tokens` and stopped mid-response.\n"
            "> Raise the cap in `presets/<lang>/{preset}.md` "
            "(e.g. `output_budget_tokens: 4000`) and re-run with `--no-cache`.\n\n"
        ),
        "ru": (
            "> ⚠️ **Вывод обрезан.** Модель упёрлась в "
            "`output_budget_tokens` и остановилась на полуслове.\n"
            "> Поднимите лимит в `presets/<lang>/{preset}.md` "
            "(например, `output_budget_tokens: 4000`) и перезапустите с `--no-cache`.\n\n"
        ),
    },
    # ---- Saved-report metadata block (analyzer/commands.py) ------------
    # The bold-prefix style stays language-neutral; only the label text
    # flips per locale.
    "report_meta_chat": {"en": "**Chat:**", "ru": "**Чат:**"},
    "report_meta_thread": {"en": "**Thread:**", "ru": "**Топик:**"},
    "report_meta_period": {"en": "**Period:**", "ru": "**Период:**"},
    "report_meta_messages": {"en": "**Messages analyzed:**", "ru": "**Сообщений проанализировано:**"},
    "report_meta_messages_filtered": {
        "en": "from {raw} raw, −{dropped} after filter/dedupe",
        "ru": "из {raw} (−{dropped} после фильтра/дедупа)",
    },
    "report_meta_preset": {"en": "**Preset:**", "ru": "**Пресет:**"},
    "report_meta_model": {"en": "**Model:**", "ru": "**Модель:**"},
    "report_meta_model_map_phase": {
        "en": "for map phase",
        "ru": "для map-фазы",
    },
    "report_meta_chunks": {"en": "**Chunks:**", "ru": "**Чанков:**"},
    "report_meta_cache": {"en": "**Cache:**", "ru": "**Кэш:**"},
    "report_meta_cache_hits_of": {
        "en": "{hits}/{total} hits",
        "ru": "{hits}/{total} попаданий",
    },
    "report_meta_enrichment": {"en": "**Enrichment:**", "ru": "**Обогащение:**"},
    "report_meta_enrichment_detail": {"en": "**Enrichment detail:**", "ru": "**Детали обогащения:**"},
    "report_meta_cost": {"en": "**Cost:**", "ru": "**Стоимость:**"},
    "report_meta_generated": {"en": "**Generated:**", "ru": "**Создано:**"},
    "report_meta_period_unread": {
        "en": "unread / full history (no date filter)",
        "ru": "непрочитанные / вся история (без фильтра по дате)",
    },
    # ---- Wizard pickers (output/preset/enrich/period custom/msg-ref) ----
    "wiz_filter_instruction": {
        "en": "(arrow keys, type to filter)",
        "ru": "(стрелки ↑↓, вводите для фильтра)",
    },
    "wiz_search_all_dialogs": {
        "en": "🔍  Search all dialogs (not just unread)",
        "ru": "🔍  Искать во всех диалогах (не только непрочитанные)",
    },
    "wiz_run_on_all_unread": {
        "en": "🚀  Run on ALL {n} unread chats ({total} total messages)",
        "ru": "🚀  Запустить на ВСЕХ {n} непрочитанных чатах (всего сообщений: {total})",
    },
    "wiz_ask_all_local": {
        "en": "🌐 Search ALL synced chats (local DB)",
        "ru": "🌐 Поиск по всем локальным чатам (без Telegram)",
    },
    "wiz_ask_question_prompt": {
        "en": "Type your question:",
        "ru": "Введите вопрос:",
    },
    "wiz_col_unread": {"en": "unread", "ru": "непроч."},
    "wiz_col_kind": {"en": "kind", "ru": "тип"},
    "wiz_col_last_msg": {"en": "last msg", "ru": "посл. сообщ."},
    "wiz_col_folder": {"en": "folder", "ru": "папка"},
    "wiz_col_title": {"en": "title", "ru": "название"},
    "wiz_pick_preset_q": {"en": "Pick a preset", "ru": "Выберите пресет"},
    "wiz_output_q": {"en": "Where do you want the output?", "ru": "Куда сохранить результат?"},
    "wiz_output_save_default": {
        "en": "📁 Save to reports/ (default, auto-named)",
        "ru": "📁 Сохранить в reports/ (по умолчанию, авто-имя)",
    },
    "wiz_output_save_custom": {
        "en": "📝 Save to custom path…",
        "ru": "📝 Сохранить по своему пути…",
    },
    "wiz_output_console": {
        "en": "🖥  Print to terminal (rendered markdown)",
        "ru": "🖥  Вывести в терминал (как markdown)",
    },
    "wiz_output_custom_prompt": {
        "en": "Custom output path (file or directory; blank = cancel)",
        "ru": "Свой путь (файл или директория; пусто — отмена)",
    },
    "wiz_enrich_q": {
        "en": "Enrich media? (→ / space to toggle, ← to uncheck, Enter to accept)",
        "ru": "Обогащать медиа? (→ / пробел — переключить, ← — снять, Enter — подтвердить)",
    },
    "wiz_enrich_voice": {"en": "Voice messages — transcribe", "ru": "Голосовые — транскрибировать"},
    "wiz_enrich_videonote": {
        "en": "Video notes (round videos) — transcribe",
        "ru": "Видеосообщения (кружки) — транскрибировать",
    },
    "wiz_enrich_link": {
        "en": "External URLs — fetch and summarize",
        "ru": "Внешние ссылки — загрузить и суммировать",
    },
    "wiz_enrich_video": {
        "en": "Videos — transcribe audio track",
        "ru": "Видео — транскрибировать звуковую дорожку",
    },
    "wiz_enrich_image": {
        "en": "Photos — describe via vision model (spendy)",
        "ru": "Фото — описать через vision-модель (дорого)",
    },
    "wiz_enrich_doc": {
        "en": "Documents (PDF / DOCX / text) — extract text",
        "ru": "Документы (PDF / DOCX / текст) — извлечь текст",
    },
    "wiz_enrich_in_db": {"en": "{n} in db", "ru": "в БД: {n}"},
    "wiz_enrich_summary_none": {"en": "none", "ru": "ничего"},
    "wiz_msg_ref_prompt": {
        "en": "Message link or msg_id (e.g. https://t.me/c/1234567/890, https://t.me/somegroup/890, or bare 890 — blank to cancel)",
        "ru": "Ссылка на сообщение или msg_id (например, https://t.me/c/1234567/890, https://t.me/somegroup/890 или просто 890 — пусто для отмены)",
    },
    "wiz_msg_ref_cant_parse": {
        "en": "Can't parse '{raw}': {err}",
        "ru": "Не удалось распарсить '{raw}': {err}",
    },
    "wiz_msg_ref_no_msgid": {
        "en": "No msg_id in '{raw}'. Expected a message link like https://t.me/c/<chat>/<msg> (optionally with a topic in between), or a bare integer id.",
        "ru": "В '{raw}' нет msg_id. Ожидалась ссылка на сообщение вида https://t.me/c/<chat>/<msg> (опционально с топиком), либо целое число id.",
    },
    "wiz_custom_since_prompt": {
        "en": "From (YYYY-MM-DD, blank for open)",
        "ru": "С даты (YYYY-MM-DD, пусто — без ограничения)",
    },
    "wiz_custom_until_prompt": {
        "en": "Until (YYYY-MM-DD, blank for open)",
        "ru": "По дату (YYYY-MM-DD, пусто — без ограничения)",
    },
    "wiz_bad_date": {
        "en": "Bad date: {value} (expected YYYY-MM-DD)",
        "ru": "Неверная дата: {value} (ожидалось YYYY-MM-DD)",
    },
    "wiz_pick_chat_from_n": {
        "en": "Pick a chat from {n} dialogs",
        "ru": "Выберите чат из {n} диалогов",
    },
    "wiz_step_back_marker": {"en": "← Back", "ru": "← Назад"},
    "wiz_summary_step_output": {"en": "output", "ru": "вывод"},
    "wiz_summary_step_console": {"en": "console", "ru": "консоль"},
    "wiz_summary_step_reports_dir": {"en": "reports/", "ru": "reports/"},
    "wiz_summary_step_auto_named": {"en": "(auto-named)", "ru": "(авто-имя)"},
    "wiz_summary_step_preset": {"en": "preset", "ru": "пресет"},
    "wiz_summary_step_period": {"en": "period", "ru": "период"},
    "wiz_summary_step_period_from": {"en": "from {ref}", "ru": "от {ref}"},
    "wiz_summary_step_enrich": {"en": "enrich", "ru": "обогащение"},
    "wiz_summary_step_mark_read": {"en": "mark-read", "ru": "пометить прочит."},
    "wiz_summary_yes": {"en": "yes", "ru": "да"},
    "wiz_summary_no": {"en": "no", "ru": "нет"},
    "wiz_summary_step_chat": {"en": "chat", "ru": "чат"},
    "wiz_summary_chat_all_unread": {"en": "ALL unread chats", "ru": "ВСЕ непрочитанные чаты"},
    "wiz_summary_chat_searching_all": {
        "en": "(searching all dialogs)",
        "ru": "(ищу во всех диалогах)",
    },
    "wiz_summary_step_mode": {"en": "mode", "ru": "режим"},
    "wiz_summary_mode_per_topic": {
        "en": "per-topic",
        "ru": "по топикам",
    },
    "wiz_summary_mode_per_topic_hint": {
        "en": "(one report per topic)",
        "ru": "(один отчёт на топик)",
    },
    "wiz_summary_mode_all_flat": {
        "en": "all-flat",
        "ru": "все вместе",
    },
    "wiz_summary_mode_all_flat_hint": {
        "en": "(whole forum as one analysis)",
        "ru": "(весь форум как один анализ)",
    },
    "wiz_summary_step_topic": {"en": "topic", "ru": "топик"},
    "wiz_summary_period_unread": {
        "en": "unread (since Telegram read marker)",
        "ru": "непрочитанные (с метки прочтения Telegram)",
    },
    "wiz_summary_period_last7": {"en": "last 7 days", "ru": "последние 7 дней"},
    "wiz_summary_period_last30": {"en": "last 30 days", "ru": "последние 30 дней"},
    "wiz_summary_period_full": {"en": "full history", "ru": "вся история"},
    "wiz_summary_period_custom": {"en": "custom range", "ru": "свой диапазон"},
    "wiz_summary_period_from_msg": {
        "en": "from a specific message",
        "ru": "с конкретного сообщения",
    },
    "wiz_summary_period_with_count": {
        "en": "{label} [{n} msgs]",
        "ru": "{label} [сообщ.: {n}]",
    },
    # ---- Confirm-step plan summary -------------------------------------
    "wiz_plan_label": {"en": "Plan", "ru": "План"},
    "wiz_plan_all_unread_chats": {
        "en": "ALL unread chats (batch)",
        "ru": "ВСЕ непрочитанные чаты (пакет)",
    },
    "wiz_plan_topic": {"en": "topic {id}", "ru": "топик {id}"},
    "wiz_plan_all_flat": {"en": "all-flat", "ru": "все-вместе"},
    "wiz_plan_per_topic": {"en": "per-topic", "ru": "по-топикам"},
    "wiz_plan_preset_kv": {"en": "preset={preset}", "ru": "пресет={preset}"},
    "wiz_plan_enrich_kv": {"en": "enrich={kinds}", "ru": "обогащение={kinds}"},
    "wiz_plan_enrich_none": {"en": "enrich=none", "ru": "обогащение=ничего"},
    "wiz_plan_period_kv": {"en": "period={period}", "ru": "период={period}"},
    "wiz_plan_range_with_count": {
        "en": "({range}, {n} msgs)",
        "ru": "({range}, сообщ.: {n})",
    },
    "wiz_plan_range": {"en": "({range})", "ru": "({range})"},
    "wiz_plan_from": {"en": "(from {ref})", "ru": "(от {ref})"},
    "wiz_plan_console": {"en": "console", "ru": "консоль"},
    "wiz_plan_file_kv": {"en": "file={path}", "ru": "файл={path}"},
    "wiz_plan_save_reports": {"en": "save to reports/", "ru": "сохранить в reports/"},
    "wiz_plan_mark_read": {"en": "mark-read", "ru": "пометить прочит."},
    "wiz_plan_for_period_synced": {
        "en": "→ For the chosen period: {total} message(s) already synced{extras}. Backfill at run time may add more.",
        "ru": "→ Для выбранного периода: уже синхронизировано сообщений: {total}{extras}. Бэкфилл при запуске может добавить ещё.",
    },
    "wiz_plan_with_media": {"en": "{n} with media", "ru": "{n} с медиа"},
    "wiz_plan_with_urls": {"en": "{n} with URLs", "ru": "{n} со ссылками"},
    "wiz_plan_msgs_approx": {"en": "messages ≈", "ru": "сообщ. ≈"},
    "wiz_plan_cost_approx": {"en": "cost ≈", "ru": "стоим. ≈"},
    "wiz_plan_pricing_missing": {
        "en": "(pricing table missing a model — cost unknown)",
        "ru": "(в таблице цен нет модели — стоимость неизвестна)",
    },
    "wiz_plan_analysis_estimate": {
        "en": "(analysis only; rough estimate)",
        "ru": "(только анализ; грубая оценка)",
    },
    "wiz_plan_extra_enrich_label": {
        "en": "+ enrichment on:",
        "ru": "+ обогащение для:",
    },
    "wiz_plan_extra_enrich_hint": {
        "en": "(adds ~$0.003/min of audio, ~$0.0002/photo, ~$0.0001/link; actual cost per run visible in",
        "ru": "(добавляет ~$0.003/мин аудио, ~$0.0002/фото, ~$0.0001/ссылка; точная стоимость на запуск в",
    },
    "wiz_plan_extra_enrich_hint_close": {"en": ")", "ru": ")"},
    "wiz_plan_zero_msgs": {
        "en": "0 messages in this period — nothing to analyze.",
        "ru": "0 сообщений за этот период — анализировать нечего.",
    },
    "wiz_plan_dump_free": {"en": "(dump is free — no OpenAI).", "ru": "(dump бесплатный — без OpenAI)."},
    # ---- runner.py: `atg chats run` -------------------------------------
    "run_no_enabled_subs": {
        "en": "No enabled subscriptions to run.",
        "ru": "Нет включённых подписок для запуска.",
    },
    "run_summary_table_title": {
        "en": "Enabled subscriptions ({n})",
        "ru": "Включённые подписки ({n})",
    },
    "run_col_title": {"en": "title", "ru": "название"},
    "run_col_kind": {"en": "kind", "ru": "тип"},
    "run_col_preset": {"en": "preset", "ru": "пресет"},
    "run_col_period": {"en": "period", "ru": "период"},
    "run_col_comments": {"en": "comments", "ru": "комментарии"},
    "run_col_chat_id": {"en": "chat_id", "ru": "chat_id"},
    "run_col_enrich": {"en": "enrich", "ru": "обогащение"},
    "run_col_mark_read": {"en": "mark_read", "ru": "пометить прочит."},
    "run_col_post_to": {"en": "post_to", "ru": "куда отправить"},
    "run_col_status": {"en": "status", "ru": "статус"},
    "run_col_note": {"en": "note", "ru": "примечание"},
    "run_folded_in_label": {"en": "✓ folded in", "ru": "✓ объединено"},
    "run_dash": {"en": "—", "ru": "—"},
    "run_default_preset": {"en": "summary", "ru": "summary"},
    "run_default_period": {"en": "unread", "ru": "unread"},
    "run_comments_auto_merge": {
        "en": "→ {n} channel(s) have a sibling comments group subscribed and will be merged into one report each.",
        "ru": "→ {n} канал(ов) имеют связанные группы комментариев — будут объединены в один отчёт на канал.",
    },
    "run_mode_picker_q": {"en": "How to run?", "ru": "Как запустить?"},
    "run_mode_per_chat": {
        "en": "📄  Per-chat — one report per subscription (default)",
        "ru": "📄  По чатам — один отчёт на подписку (по умолчанию)",
    },
    "run_mode_flat": {
        "en": "📚  Flat — single combined report across all enabled subs",
        "ru": "📚  Плоский режим — один объединённый отчёт по всем подпискам",
    },
    "run_mode_cancel": {"en": "← Cancel", "ru": "← Отмена"},
    "run_plan_title": {
        "en": "`atg chats run` plan — {n} subscription(s)",
        "ru": "План `atg chats run` — подписок: {n}",
    },
    "run_flat_plan_title": {
        "en": "`atg chats run --flat` plan — {n} subscription(s)",
        "ru": "План `atg chats run --flat` — подписок: {n}",
    },
    "run_results_title": {"en": "`atg chats run` results", "ru": "Результаты `atg chats run`"},
    "run_enrich_none": {"en": "none", "ru": "ничего"},
    "run_enrich_all": {"en": "all", "ru": "всё"},
    "run_enrich_config_defaults": {
        "en": "(config defaults)",
        "ru": "(по умолчанию из конфига)",
    },
    "run_comments_merge_note": {
        "en": "→ {n} channel(s) will have their linked discussion-group comments pulled into the same report (one merged analysis per channel, not two separate reports).",
        "ru": "→ {n} канал(ов) подтянут комментарии из связанной группы в тот же отчёт (один объединённый анализ на канал, а не два отдельных).",
    },
    "run_dry_run_note": {"en": "→ --dry-run: not running.", "ru": "→ --dry-run: запуск пропущен."},
    "run_analyze_confirm_q": {
        "en": "Run analyze on {n} subscription(s)?",
        "ru": "Запустить анализ на {n} подписк(ах)?",
    },
    "run_flat_confirm_q": {
        "en": "Build one merged report from {n} chat(s)?",
        "ru": "Собрать один объединённый отчёт из {n} чат(ов)?",
    },
    "run_progress_line": {
        "en": "[{i}/{total}] {title} (preset={preset}, period={period})",
        "ru": "[{i}/{total}] {title} (пресет={preset}, период={period})",
    },
    "run_skipped_no_msgs": {"en": "skipped (no msgs)", "ru": "пропущено (нет сообщений)"},
    "run_exit_code_label": {"en": "exit {code}", "ru": "код выхода {code}"},
    "run_status_ok": {"en": "OK", "ru": "ОК"},
    "run_status_fail": {"en": "FAIL", "ru": "ОШИБКА"},
    "run_results_summary": {
        "en": "{ok}/{total} succeeded.",
        "ru": "Успешно: {ok}/{total}.",
    },
    "run_unknown_preset": {"en": "Unknown preset:", "ru": "Неизвестный пресет:"},
    "run_flat_mode_desc": {
        "en": "→ Flat mode: one combined report. preset={preset}, period={period}, enrich={enrich}.",
        "ru": "→ Плоский режим: один объединённый отчёт. пресет={preset}, период={period}, обогащение={enrich}.",
    },
    "run_flat_sub_progress": {
        "en": "[{i}/{total}] {title} ({kind}{maybe_comments})",
        "ru": "[{i}/{total}] {title} ({kind}{maybe_comments})",
    },
    "run_flat_with_comments_suffix": {
        "en": ", + comments",
        "ru": ", + комментарии",
    },
    "run_flat_no_msgs": {
        "en": "→ {title}: no messages, skipped.",
        "ru": "→ {title}: сообщений нет, пропущено.",
    },
    "run_flat_zero_msgs": {
        "en": "→ {title}: 0 messages.",
        "ru": "→ {title}: 0 сообщений.",
    },
    "run_no_msgs_across_subs": {
        "en": "No messages across any sub.",
        "ru": "Нет сообщений ни в одной подписке.",
    },
    "run_flat_title": {
        "en": "All chats (flat) — {n_chats} chat(s), {n_msgs} msg(s)",
        "ru": "Все чаты (плоский режим) — чатов: {n_chats}, сообщений: {n_msgs}",
    },
    "run_flat_analyzing": {
        "en": "→ Running combined analysis on {n} merged message(s)...",
        "ru": "→ Запуск объединённого анализа по {n} сообщ...",
    },
    "run_flat_failed": {"en": "Flat run failed:", "ru": "Плоский запуск не удался:"},
    "run_flat_report_h1": {
        "en": "# Flat run — {n_chats} chat(s)",
        "ru": "# Плоский запуск — чатов: {n_chats}",
    },
    "run_flat_report_meta": {
        "en": "_Preset {preset}; period {period}; {n_msgs} message(s) merged; cost ${cost}_",
        "ru": "_Пресет {preset}; период {period}; объединено сообщ.: {n_msgs}; стоимость ${cost}_",
    },
    "run_flat_per_chat_h2": {"en": "## Per-chat counts", "ru": "## Сообщений по чатам"},
    "run_flat_kind_channel": {"en": "channel", "ru": "канал"},
    "run_flat_comments_label": {
        "en": "{n} comments ({title})",
        "ru": "{n} комментариев ({title})",
    },
    "run_flat_comments_fallback_title": {
        "en": "comments {chat_id}",
        "ru": "комментарии {chat_id}",
    },
    "run_saved_label": {"en": "Saved", "ru": "Сохранено"},
    "run_post_to_failed": {"en": "post-to failed:", "ru": "отправка не удалась:"},
    "run_marked_read_across": {
        "en": "→ Marked read across {n} dialog(s)/topic(s).",
        "ru": "→ Помечено как прочитанное в {n} диалог(ах)/топик(ах).",
    },
    # ---- cli.py: cache / cleanup / prune / folders ----------------------
    "cli_ref_or_chat_required": {
        "en": "Provide a chat reference or --chat <id>.",
        "ru": "Укажите ссылку на чат либо --chat <id>.",
    },
    "cli_no_folders": {
        "en": "No folders defined in this Telegram account.",
        "ru": "В этом аккаунте Telegram не определены папки.",
    },
    "cli_folders_table_title": {"en": "Telegram folders", "ru": "Папки Telegram"},
    "cli_folder_col_id": {"en": "id", "ru": "id"},
    "cli_folder_col_title": {"en": "title", "ru": "название"},
    "cli_folder_col_icon": {"en": "icon", "ru": "иконка"},
    "cli_folder_col_chats": {"en": "chats", "ru": "чаты"},
    "cli_folder_col_kind": {"en": "kind", "ru": "тип"},
    "cli_folder_kind_chatlist": {"en": "chatlist", "ru": "chatlist"},
    "cli_folder_kind_rule_based": {"en": "rule-based", "ru": "по правилам"},
    "cli_folder_kind_explicit": {"en": "explicit", "ru": "явный"},
    "cli_folders_use_with": {
        "en": 'Use with: atg analyze --folder "Alpha" — batch-analyze unread chats in that folder.',
        "ru": 'Использование: atg analyze --folder "Alpha" — пакетный анализ непрочитанных чатов в этой папке.',
    },
    "cli_skipped_label": {"en": "Skipped", "ru": "Пропущено"},
    "cli_cache_purge_min_days": {
        "en": "cache purge: --older-than must be greater than 0 days.",
        "ru": "очистка кэша: --older-than должен быть больше 0 дней.",
    },
    "cli_purged_label": {"en": "Purged", "ru": "Удалено"},
    "cli_cache_purged_msg": {
        "en": "{n} analysis_cache rows older than {days} days.",
        "ru": "{n} строк analysis_cache старше {days} дн.",
    },
    "cli_vacuumed_label": {"en": "Vacuumed", "ru": "VACUUM"},
    "cli_db_vacuumed_msg": {"en": "DB — reclaimed {size}.", "ru": "БД — освобождено {size}."},
    "cli_no_usage_label": {"en": "No usage logged yet", "ru": "Лог использования пуст"},
    "cli_no_usage_hint": {"en": "run an analyze first.", "ru": "сначала запустите analyze."},
    "cli_cache_eff_title": {"en": "Cache effectiveness{since}", "ru": "Эффективность кэша{since}"},
    "cli_cache_eff_since": {"en": " since {date}", "ru": " с {date}"},
    "cli_cache_col_chat_id": {"en": "chat_id", "ru": "chat_id"},
    "cli_cache_col_preset": {"en": "preset", "ru": "пресет"},
    "cli_cache_col_calls": {"en": "calls", "ru": "вызовов"},
    "cli_cache_col_hit_calls": {"en": "hit calls", "ru": "попаданий"},
    "cli_cache_col_hit_rate": {"en": "hit rate", "ru": "процент попад."},
    "cli_cache_col_prompt_tok": {"en": "prompt tok", "ru": "промпт ток."},
    "cli_cache_col_cached_tok": {"en": "cached tok", "ru": "кэш. ток."},
    "cli_cache_col_cost": {"en": "cost $", "ru": "стоимость $"},
    "cli_cache_eff_hint": {
        "en": "Hit rate counts OpenAI server-side prompt-cache reuse (`cached_tokens / prompt_tokens`). Local analysis_cache hits aren't logged (they cost zero) — see `atg cache stats` for that table.",
        "ru": "Процент попаданий считает переиспользование серверного prompt-cache OpenAI (`cached_tokens / prompt_tokens`). Локальные попадания analysis_cache не логируются (они бесплатны) — для этой таблицы см. `atg cache stats`.",
    },
    "cli_cache_empty": {"en": "analysis_cache is empty.", "ru": "analysis_cache пуст."},
    "cli_cache_summary": {
        "en": "{rows} rows, {size} of result text, saved ~${saved} in re-runs.\nAge range: {oldest}  →  {newest}",
        "ru": "{rows} строк, {size} текста результатов, сэкономлено ~${saved} на повторных запусках.\nДиапазон возраста: {oldest}  →  {newest}",
    },
    "cli_cache_by_group_title": {"en": "By (preset, model)", "ru": "По (пресет, модель)"},
    "cli_cache_col_model": {"en": "model", "ru": "модель"},
    "cli_cache_col_rows": {"en": "rows", "ru": "строк"},
    "cli_cache_col_size": {"en": "size", "ru": "размер"},
    "cli_cache_col_saved": {"en": "saved $", "ru": "сэкономлено $"},
    "cli_cache_col_hash": {"en": "hash", "ru": "хеш"},
    "cli_cache_col_ver": {"en": "ver", "ru": "версия"},
    "cli_cache_col_cost_short": {"en": "cost", "ru": "стоим."},
    "cli_cache_col_created_at": {"en": "created_at", "ru": "создано"},
    "cli_cache_no_matches": {"en": "No matching entries.", "ru": "Совпадений нет."},
    "cli_cache_no_entry": {
        "en": "No entry matching {hash}.",
        "ru": "Нет записей по {hash}.",
    },
    "cli_cache_no_entry_label": {"en": "No entry matching", "ru": "Нет записей по"},
    "cli_cache_ambiguous_label": {"en": "Ambiguous prefix", "ru": "Неоднозначный префикс"},
    "cli_cache_ambiguous_msg": {
        "en": "{n} matches. Use a longer prefix.",
        "ru": "совпадений: {n}. Укажите более длинный префикс.",
    },
    "cli_unknown_format_label": {"en": "Unknown format", "ru": "Неизвестный формат"},
    "cli_unknown_format_msg": {
        "en": "{fmt}. Use jsonl or md.",
        "ru": "{fmt}. Допустимо: jsonl или md.",
    },
    "cli_export_no_matches": {
        "en": "No matching entries — nothing written.",
        "ru": "Совпадений нет — ничего не записано.",
    },
    "cli_wrote_label": {"en": "Wrote", "ru": "Записано"},
    "cli_export_wrote_msg": {
        "en": "{n} entries → {path} ({fmt}).",
        "ru": "{n} записей → {path} ({fmt}).",
    },
    "cli_cleanup_nothing": {
        "en": "Nothing to redact",
        "ru": "Нечего обезличивать",
    },
    "cli_cleanup_older_than": {
        "en": "older than {days} days.",
        "ru": "старше {days} дн.",
    },
    "cli_cleanup_already_clean_label": {"en": "Already clean", "ru": "Уже чисто"},
    "cli_cleanup_already_clean_msg": {
        "en": "{n} matching rows older than {days} days, but nothing left to null (text already NULL{tail}).",
        "ru": "{n} подходящих строк старше {days} дн., но обнулять уже нечего (текст уже NULL{tail}).",
    },
    "cli_cleanup_transcripts_kept": {
        "en": "; transcripts kept",
        "ru": "; транскрипты сохранены",
    },
    "cli_cleanup_preview_title": {"en": "Cleanup preview", "ru": "Предпросмотр очистки"},
    "cli_cleanup_preview_scope_chat": {"en": "chat={chat}", "ru": "чат={chat}"},
    "cli_cleanup_preview_scope_all": {"en": "all chats", "ru": "все чаты"},
    "cli_cleanup_preview_lines": {
        "en": "  messages matched:        {messages}\n  [red]rows to redact[/]:          {to_redact}\n  [red]text to null-out[/]:        {with_text}\n  transcripts to null-out: {transcripts}\n[dim]Row metadata (ids, dates, authors) is preserved.[/]",
        "ru": "  совпавших сообщений:        {messages}\n  [red]строк к обезличиванию[/]:    {to_redact}\n  [red]текста к обнулению[/]:        {with_text}\n  транскриптов к обнулению: {transcripts}\n[dim]Метаданные (ids, даты, авторы) сохраняются.[/]",
    },
    "cli_cleanup_kept_label": {"en": "(kept)", "ru": "(сохранено)"},
    "cli_cleanup_proceed_q": {
        "en": "Proceed with redaction?",
        "ru": "Продолжить обезличивание?",
    },
    "cli_aborted": {"en": "Aborted.", "ru": "Прервано."},
    "cli_redacted_label": {"en": "Redacted", "ru": "Обезличено"},
    "cli_redacted_msg": {
        "en": "{n} messages older than {days} days{tail}.",
        "ru": "{n} сообщений старше {days} дн.{tail}.",
    },
    "cli_redacted_transcripts_kept": {
        "en": " (transcripts kept)",
        "ru": " (транскрипты сохранены)",
    },
    "cli_prune_min_days": {
        "en": "— --older-than must be > 0 days.",
        "ru": "— --older-than должен быть > 0 дн.",
    },
    "cli_prune_no_root_label": {"en": "No reports root", "ru": "Нет корневой папки отчётов"},
    "cli_prune_no_root_msg": {
        "en": "at {path} — nothing to prune.",
        "ru": "по пути {path} — чистить нечего.",
    },
    "cli_prune_nothing_old": {
        "en": "Nothing older than {days} days under {root}.",
        "ru": "Под {root} нет файлов старше {days} дн.",
    },
    "cli_prune_verb_would_delete": {"en": "Would delete", "ru": "Было бы удалено"},
    "cli_prune_verb_would_trash": {"en": "Would trash", "ru": "Было бы в корзину"},
    "cli_prune_verb_delete": {"en": "Delete", "ru": "Удалить"},
    "cli_prune_verb_trash": {"en": "Trash", "ru": "В корзину"},
    "cli_prune_summary": {
        "en": "{n} file(s) ({size}) older than {days} days under {root}.",
        "ru": "файл(ов): {n} ({size}) старше {days} дн. под {root}.",
    },
    "cli_prune_and_more": {"en": "… and {n} more", "ru": "… и ещё {n}"},
    "cli_prune_proceed_q": {"en": "Proceed?", "ru": "Продолжить?"},
    "cli_prune_failed_delete_label": {"en": "Failed to delete", "ru": "Не удалось удалить"},
    "cli_prune_deleted_label": {"en": "Deleted", "ru": "Удалено"},
    "cli_prune_deleted_msg": {"en": "{n} file(s).", "ru": "файл(ов): {n}."},
    "cli_prune_failed_move_label": {"en": "Failed to move", "ru": "Не удалось переместить"},
    "cli_prune_trashed_label": {"en": "Trashed", "ru": "В корзине"},
    "cli_prune_trashed_msg": {
        "en": "{n} file(s) → {path}",
        "ru": "файл(ов): {n} → {path}",
    },
    # ---- watch / backup / restore ---------------------------------------
    "cli_watch_need_inner": {
        "en": "Pass an inner command, e.g. atg watch --interval 1h analyze --folder Work",
        "ru": "Передайте внутреннюю команду, например: atg watch --interval 1h analyze --folder Work",
    },
    "cli_watch_interval_positive": {
        "en": "--interval must be > 0.",
        "ru": "--interval должен быть > 0.",
    },
    "cli_watch_watching": {
        "en": "Watching every {interval}: {cmd}",
        "ru": "Слежу каждые {interval}: {cmd}",
    },
    "cli_watch_run_n": {"en": "── Run {n}", "ru": "── Запуск {n}"},
    "cli_watch_inner_exited": {
        "en": "Inner exited with code {code}",
        "ru": "Внутренняя команда завершилась с кодом {code}",
    },
    "cli_watch_not_on_path": {
        "en": "`{cmd}` not on PATH.",
        "ru": "`{cmd}` отсутствует в PATH.",
    },
    "cli_watch_max_runs_reached": {
        "en": "Hit --max-runs {n}; exiting.",
        "ru": "Достигнут лимит --max-runs {n}; выходим.",
    },
    "cli_watch_sleeping": {"en": "Sleeping {interval}...", "ru": "Спим {interval}..."},
    "cli_watch_interrupted": {"en": "Interrupted; exiting.", "ru": "Прервано; выходим."},
    "cli_watch_invalid_duration": {
        "en": "Invalid duration: {value}",
        "ru": "Неверная длительность: {value}",
    },
    "cli_backup_no_db": {
        "en": "No DB at {path} — nothing to back up.",
        "ru": "По пути {path} нет БД — нечего резервировать.",
    },
    "cli_backup_already_exists": {
        "en": "{path} already exists. Pass --overwrite or pick a different path.",
        "ru": "{path} уже существует. Укажите --overwrite или выберите другой путь.",
    },
    "cli_backup_done_label": {"en": "Backed up", "ru": "Сохранено"},
    "cli_restore_not_found_label": {"en": "Backup not found:", "ru": "Резервная копия не найдена:"},
    "cli_restore_confirm_q": {
        "en": "Replace {dst} with {src}? Current DB will be moved aside.",
        "ru": "Заменить {dst} на {src}? Текущая БД будет отложена в сторону.",
    },
    "cli_restore_moved_db": {
        "en": "Moved current DB to {path}",
        "ru": "Текущая БД перемещена в {path}",
    },
    "cli_restore_done_label": {"en": "Restored", "ru": "Восстановлено"},
    # ---- tg/commands.py: dialogs / describe / chats add / refresh -------
    "tg_doctor_banner": {"en": "analyzetg doctor", "ru": "analyzetg doctor"},
    "tg_n_rows": {"en": "{n} row(s)", "ru": "строк: {n}"},
    "tg_listing_dialogs": {
        "en": "→ Listing dialogs...",
        "ru": "→ Получаем список диалогов...",
    },
    "tg_describe_hint": {
        "en": "Use `describe <ref>` for details on one chat.",
        "ru": "Команда `describe <ref>` покажет детали одного чата.",
    },
    "tg_describe_id_kind": {
        "en": "(id={chat_id}, kind={kind})",
        "ru": "(id={chat_id}, тип={kind})",
    },
    "tg_top_senders_label": {"en": "Top senders", "ru": "Топ авторов"},
    "tg_no_messages_local": {
        "en": "Local DB: no messages stored for this chat yet.",
        "ru": "Локальная БД: для этого чата ещё нет сохранённых сообщений.",
    },
    "tg_not_a_forum": {
        "en": "{title} is not a forum group.",
        "ru": "{title} — не форум-группа.",
    },
    "tg_n_topics": {"en": "{n} topic(s)", "ru": "топиков: {n}"},
    "tg_resolve_parsed_label": {"en": "Parsed:", "ru": "Распознано:"},
    "tg_resolve_done_label": {"en": "Resolved:", "ru": "Резолв:"},
    "tg_resolve_failed_label": {"en": "Resolve failed:", "ru": "Резолв не удался:"},
    "tg_describe_id_kind_inline": {
        "en": "(id={chat_id}, kind={kind})",
        "ru": "(id={chat_id}, тип={kind})",
    },
    "tg_describe_participants": {
        "en": "  participants: {n}",
        "ru": "  участников: {n}",
    },
    "tg_describe_linked_chat_id": {
        "en": "  linked_chat_id: {id}",
        "ru": "  linked_chat_id: {id}",
    },
    "tg_describe_about": {"en": "  about: {text}", "ru": "  описание: {text}"},
    "tg_channel_no_linked": {
        "en": "{title} has no linked discussion group.",
        "ru": "{title} — нет связанной группы обсуждений.",
    },
    "tg_channel_label": {"en": "Channel", "ru": "Канал"},
    "tg_added_label": {"en": "Added", "ru": "Добавлено"},
    "tg_added_msg": {"en": "{n} subscription(s).", "ru": "подписок: {n}."},
    "tg_added_sub_line": {
        "en": "  - chat={chat_id} thread={thread_id} kind={kind} title={title}",
        "ru": "  - chat={chat_id} thread={thread_id} тип={kind} название={title}",
    },
    "tg_last_take_effect": {
        "en": "--last {value} will take effect on next sync (start from newest-N).",
        "ru": "--last {value} вступит в силу при следующей синхронизации (с N последних).",
    },
    "tg_no_matching_subs": {
        "en": "No matching subscriptions.",
        "ru": "Подходящих подписок нет.",
    },
    "tg_done_label": {"en": "Done.", "ru": "Готово."},
    "tg_done_n_msgs": {"en": "{n} message(s).", "ru": "сообщений: {n}."},
    "tg_from_msg_must_be_link_or_id": {
        "en": "--from-msg must be a message link or msg_id.",
        "ru": "--from-msg должен быть ссылкой на сообщение или msg_id.",
    },
    "tg_backfilled_label": {"en": "Backfilled", "ru": "Догружено"},
    "tg_backfilled_msg": {
        "en": "{n} message(s) chat={chat} direction={direction}.",
        "ru": "сообщений: {n} чат={chat} направление={direction}.",
    },
    "tg_resolve_multiple_candidates": {
        "en": "Multiple candidates, pick one:",
        "ru": "Несколько кандидатов, выберите одного:",
    },
    "tg_resolve_candidate_line": {
        "en": "  [{i}] {title} @{username} (score {score}, {kind})",
        "ru": "  [{i}] {title} @{username} (балл {score}, {kind})",
    },
    # ---- chats list / chats manage --------------------------------------
    "tg_dialogs_default_filter": {
        "en": "default filter: unread + forums/groups/supergroups",
        "ru": "фильтр по умолчанию: непрочитанные + форумы/группы/супергруппы",
    },
    "tg_dialogs_pass_all": {
        "en": "pass --all to see everything",
        "ru": "укажите --all, чтобы видеть всё",
    },
    "tg_chats_no_subs": {
        "en": "No subscriptions yet.",
        "ru": "Подписок пока нет.",
    },
    "tg_chats_use_add": {
        "en": "Use [cyan]atg chats add[/] to create one.",
        "ru": "Используйте [cyan]atg chats add[/], чтобы создать.",
    },
    "tg_chats_done_label": {"en": "← Done", "ru": "← Готово"},
    "tg_chats_manage_q": {
        "en": "Manage subscriptions ({n} total) — pick one:",
        "ru": "Управление подписками (всего: {n}) — выберите одну:",
    },
    "tg_sub_state_on": {"en": "[on]", "ru": "[вкл]"},
    "tg_sub_state_off": {"en": "[off]", "ru": "[выкл]"},
    "tg_sub_thread_label": {"en": "thread={id}", "ru": "топик={id}"},
    "tg_sub_gone": {"en": "Subscription gone:", "ru": "Подписки больше нет:"},
    "tg_sub_what_next_q": {"en": "{title} — what next?", "ru": "{title} — что дальше?"},
    "tg_sub_action_disable": {"en": "Disable", "ru": "Отключить"},
    "tg_sub_action_enable": {"en": "Enable", "ru": "Включить"},
    "tg_sub_action_remove_keep": {
        "en": "Remove (keep messages)",
        "ru": "Удалить (сохранить сообщения)",
    },
    "tg_sub_action_remove_purge": {
        "en": "Remove + delete stored messages",
        "ru": "Удалить + стереть сохранённые сообщения",
    },
    "tg_sub_back_label": {"en": "← Back", "ru": "← Назад"},
    "tg_sub_disabled": {
        "en": "→ Disabled chat={chat_id} thread={thread_id}",
        "ru": "→ Отключено chat={chat_id} thread={thread_id}",
    },
    "tg_sub_enabled": {
        "en": "→ Enabled chat={chat_id} thread={thread_id}",
        "ru": "→ Включено chat={chat_id} thread={thread_id}",
    },
    "tg_sub_purge_confirm_q": {
        "en": "Delete ALL stored messages for chat={chat_id}? Cannot be undone.",
        "ru": "Удалить ВСЕ сохранённые сообщения для chat={chat_id}? Действие необратимо.",
    },
    "tg_sub_purge_skipped": {
        "en": "Skipped — kept the subscription.",
        "ru": "Пропущено — подписка сохранена.",
    },
    "tg_sub_removed": {
        "en": "→ Removed chat={chat_id} thread={thread_id} (purged={purge})",
        "ru": "→ Удалено chat={chat_id} thread={thread_id} (purge={purge})",
    },
    # ---- ask / export / media commands ---------------------------------
    "ask_folder_label": {"en": "→ Folder", "ru": "→ Папка"},
    "ask_n_chats": {"en": "{n} chat(s)", "ru": "чат(ов): {n}"},
    "ask_asking_label": {"en": "→ Asking", "ru": "→ Спрашиваем"},
    "ask_over_n_msgs": {
        "en": "over {n} message(s)...",
        "ru": "по сообщениям: {n}...",
    },
    "export_transcribe_label": {"en": "Transcribe", "ru": "Транскрипт"},
    "export_pending_label": {"en": "pending={n}", "ru": "ожидает: {n}"},
    "export_n_msgs": {"en": "{n} message(s)", "ru": "сообщений: {n}"},
    "media_saving_label": {"en": "Saving media:", "ru": "Сохранение медиа:"},
    "media_n_files": {"en": "{n} file(s)", "ru": "файл(ов): {n}"},
    "media_resolving_label": {"en": "→ Resolving", "ru": "→ Резолвим"},
    "media_no_matching": {"en": "No media matching filters.", "ru": "Медиа по фильтрам не найдено."},
    "media_plan_label": {"en": "Plan:", "ru": "План:"},
    "media_dry_run_no_files": {
        "en": "Dry run — no files written.",
        "ru": "Dry run — файлы не записаны.",
    },
    "tg_resolve_index_prompt": {
        "en": "Index (Enter = top match)",
        "ru": "Индекс (Enter — лучший вариант)",
    },
    "wiz_n_topics_in_forum": {
        "en": "{n} topic(s) in this forum",
        "ru": "топиков в форуме: {n}",
    },
}


# Common ISO codes → display names used by the optional content_language
# hint and any `Respond in <X>` style append. Unknown codes degrade to
# `code.title()`.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ru": "Russian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "uk": "Ukrainian",
    "pl": "Polish",
    "tr": "Turkish",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
}


def language_name(code: str) -> str:
    """Human-readable name for an ISO code; falls back to a Title-cased code."""
    return LANGUAGE_NAMES.get(code.lower(), code.title() if code else "English")


def t(key: str, lang: str | None = None) -> str:
    """Lookup a localized string. Falls back to English if the key is
    missing for the requested language. Raises if the key is unknown.

    Pass `lang=None` (the default) to resolve from the active settings
    at call time — test monkeypatches and `--language` overrides take
    effect immediately, without any module-level caching.
    """
    if key not in _STRINGS:
        raise KeyError(f"i18n: unknown key {key!r}. Add it to analyzetg/i18n.py.")
    if lang is None:
        from analyzetg.config import get_settings

        lang = get_settings().locale.language
    bucket = _STRINGS[key]
    return bucket.get(lang, bucket["en"])


def tf(key: str, lang: str | None = None, /, **kwargs) -> str:
    """`t()` + `.format(**kwargs)` in one call.

    Common case: `tf("foo", n=3)` is a shorter way to write
    `t("foo").format(n=3)`. The lang positional is positional-only so
    callers pass kwargs cleanly: `tf("foo", n=3, item="bar")`. To pin a
    specific language, use `tf("foo", "ru", n=3)`.
    """
    return t(key, lang).format(**kwargs)
