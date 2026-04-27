"""Analysis presets — per-language directories.

Presets live under `presets/<language>/*.md`. Each language directory is
autonomous: a language can have any subset of presets. The loader does
NOT fall back across languages — if `language="en"` and `summary.md` is
missing in `presets/en/`, it's not available there. This is intentional:
each language is self-contained.

Each preset file has a YAML-ish frontmatter block (name, prompt_version,
description, models, output budget, etc.) and a body split by `---USER---`:
everything before is the system prompt, everything after is the user
template.

`prompt_version` is part of the cache key — bump it to invalidate stale
cached results when you edit a preset's body.

Public surface:
- `get_presets(language)` — `{name: Preset}` for the language directory.
- `compose_system_prompt(preset_system, *, language, ...)` — base + forum
  addendum + preset, all in the active language.
- `BASE_VERSION` — bumped whenever the composer's structural behavior
  changes; threaded into `options_payload` to bust cache for every preset.
- Module-level `PRESETS`, `BASE_SYSTEM`, `REDUCE_PROMPT` are kept as
  lazy proxies (resolved via `__getattr__`) for backward-compat with
  existing imports — they read the current `settings.locale.language` at
  access time.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRESETS_DIR = Path(__file__).resolve().parent.parent.parent / "presets"
USER_MARKER = "---USER---"


# Bumped whenever _base.md, _reduce.md, the forum addendum, or
# compose_system_prompt's structural behavior changes in a way that
# should invalidate existing analysis_cache rows. Threaded into
# `options_payload` in analyzer/pipeline.py so a base-rule change busts
# EVERY preset's cache without per-preset prompt_version bumps.
#
# v5: split language vs content_language semantics. Now `content_language`
# drives which presets/<lang>/ tree the loader reads (and thus the
# language of prompts and LLM output); `language` only drives user-facing
# UI / report headings. The compose_system_prompt signature dropped its
# old `content_language` informational hint (redundant now that prompts
# are natively in content_language). Cached rows from v4 used the old
# meaning and must be re-run.
# v6: _base.md generalized from "Telegram chats" to "chat OR video
# transcript" — adds a video addendum block and timestamp-aware citation
# guidance. compose_system_prompt now takes `source_kind`. Cached rows
# from v5 are still semantically valid for chats but get re-keyed.
BASE_VERSION = "v6"


# ---------------------------------------------------------------------------
# Per-language constants
# ---------------------------------------------------------------------------

# Default user-template tail when a preset omits its `---USER---` body.
# Per-language so the LLM sees the natural-language preamble.
DEFAULT_USER_TAIL: dict[str, str] = {
    "ru": "Период: {period}\nЧат: {title}\nСообщений: {msg_count}\n---\n{messages}",
    "en": "Period: {period}\nChat: {title}\nMessages: {msg_count}\n---\n{messages}",
}


# Forum-mode system addendum. Only emitted when the analyzer is in
# all-flat forum mode (see `compose_system_prompt`'s `is_forum` arg).
_FORUM_CONTEXT: dict[str, str] = {
    "ru": """
## Форум-режим (анализируется весь форум, а не один топик)

Сообщения сгруппированы по топикам. Перед каждой группой стоит
заголовок `=== Топик: <название> (id=<id>) ===`. Внутри группы
сообщения идут хронологически; между группами хронология не сохраняется.

**Разные топики = разные обсуждения.** Не сваливай их в одну кучу.

### Обязательное правило структуры

Когда твой формат пресета использует разделы или списки, **каждый
раздел с содержательным списком обязан быть сгруппирован по топикам
через подзаголовки третьего уровня (`###`).** Пример для раздела `## Главное`:

```
## Главное

### <название топика 1>
- инсайт 1
- инсайт 2

### <название топика 2>
- инсайт 1

### <название топика 3>
- инсайт 1
- инсайт 2
```

Правила:
- Заголовок подраздела — **название топика**, не id.
- Если в топике реально нечего выделить — пропусти его, не натягивай
  пункты ради симметрии.
- Если инсайт реально пересекает несколько топиков (редкий случай), —
  вынеси его в отдельный подраздел `### Между топиками` в конце.
- Норма — 2–4 инсайта на топик. В одноразовом саммари больше не нужно
  (это концентрат, не стенограмма).

### TL;DR в начале

Добавь в самое начало ответа короткий блок:

```
## TL;DR
_Одна-две строки: что в этом форуме произошло за период, одним движением._
```

Для занятого читателя, который откроет файл и решит, стоит ли читать дальше.
""".strip(),
    "en": """
## Forum mode (analyzing the whole forum, not a single topic)

Messages are grouped by topic. Each group is preceded by a header
`=== Topic: <name> (id=<id>) ===`. Within a group, messages are in
chronological order; chronology is NOT preserved across groups.

**Different topics = different conversations.** Don't blend them into one bucket.

### Mandatory structural rule

When your preset format uses sections or lists, **every section with a
substantive list MUST be grouped by topic via third-level subheaders
(`###`).** Example for a `## Main` section:

```
## Main

### <topic name 1>
- insight 1
- insight 2

### <topic name 2>
- insight 1

### <topic name 3>
- insight 1
- insight 2
```

Rules:
- Subheader is the **topic name**, not its id.
- If a topic genuinely has nothing worth flagging — skip it; don't
  stretch points for symmetry.
- If an insight truly spans multiple topics (rare), put it under a
  trailing `### Cross-topic` section.
- Norm: 2–4 insights per topic. A one-shot summary doesn't need more
  (this is a concentrate, not a transcript).

### TL;DR at the top

Prepend a short block to your answer:

```
## TL;DR
_One or two lines: what happened in this forum during the period, in one stroke._
```

For the busy reader who opens the file and decides whether to read on.
""".strip(),
}


# Inline fallback content used only if the corresponding `presets/<lang>/_base.md`
# (or `_reduce.md`) is missing. The on-disk files are authoritative.
_BASE_FALLBACK: dict[str, str] = {
    "ru": (
        "Ты — аналитик Telegram-чатов. Опирайся только на приведённые "
        "сообщения; не выдумывай фактов. Ссылки на сообщения формата "
        "[#<msg_id>](<link>), где link — шаблон из преамбулы. Пиши "
        "по-русски, плотно, без воды."
    ),
    "en": (
        "You analyze Telegram chats. Rely only on the provided messages; "
        "do not invent facts. Cite messages using [#<msg_id>](<link>), "
        "where link is the template from the preamble. Write in English, "
        "concisely, no fluff."
    ),
}


_REDUCE_FALLBACK: dict[str, str] = {
    "ru": (
        "Ниже — несколько уже готовых мини-саммари одного и того же чата. "
        "Слей их в одно финальное саммари. Не дублируй пункты, объединяй похожие. "
        "Пиши по-русски."
    ),
    "en": (
        "Below are several mini-summaries of the same chat from different chunks. "
        "Merge them into ONE final summary. Don't duplicate points; merge similar ones. "
        "Write in English."
    ),
}


# Default custom-preset (--prompt-file with no frontmatter) system prompt.
_CUSTOM_FALLBACK_SYSTEM: dict[str, str] = {
    "ru": (
        "Ты аналитик Telegram-чата. Следуй инструкциям ниже и отвечай по-русски, "
        "без воды, опираясь только на приведённые сообщения."
    ),
    "en": (
        "You analyze a Telegram chat. Follow the instructions below; answer in English, "
        "concisely, relying only on the provided messages."
    ),
}


# ---------------------------------------------------------------------------
# Preset dataclass + loader
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Preset:
    name: str
    prompt_version: str
    system: str
    user_template: str
    needs_reduce: bool = True
    filter_model: str = "gpt-5.4-nano"
    final_model: str = "gpt-5.4"
    output_budget_tokens: int = 1500
    # Per-chunk output cap in the map phase. Kept separate from
    # `output_budget_tokens` (which governs the final reduce output) so
    # individual chunks can produce richer mini-summaries without inflating
    # the final answer budget.
    map_output_tokens: int = 1500
    # Hard cap on per-chunk *input* tokens, applied on TOP of the model's
    # context window. Set this when a preset works with very long inputs
    # (video transcripts) and you'd rather force map-reduce earlier than
    # let one giant call hit per-minute TPM ceilings or wash out the LLM's
    # focus. None = use the model's full effective budget. Typical values:
    # 30_000-50_000 for video.
    max_chunk_input_tokens: int | None = None
    # Short one-liner shown by the wizard's preset picker. Read from the
    # preset's frontmatter so adding a new preset is metadata-only.
    description: str | None = None
    options_keys: list[str] = field(default_factory=list)
    # Media enrichments this preset wants turned on by default. Merged with
    # CLI / config toggles — see enrich.EnrichOpts and analyzer.commands.
    # Names match EnrichOpts fields: voice, videonote, video, image, doc, link.
    enrich_kinds: list[str] = field(default_factory=list)

    def render_user(self, **kw: object) -> str:
        return self.user_template.format(**kw)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split `---\\n...\\n---\\n<body>` into (meta, body). No external deps."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    block = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


def _coerce_bool(v: str) -> bool:
    return v.lower() in ("true", "yes", "1", "on")


def _coerce_list(v: str) -> list[str]:
    """Parse a simple `[a, b, c]` or `a, b, c` list from preset frontmatter."""
    s = v.strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    parts = [p.strip().strip('"').strip("'") for p in s.split(",")]
    return [p for p in parts if p]


_PIPELINE_PLACEHOLDERS = ("period", "title", "msg_count", "messages")


def _validate_user_template(user_template: str, *, path: Path) -> None:
    """Catch preset typos like `{periiod}` at load time rather than mid-run."""
    try:
        user_template.format(period="", title="", msg_count=0, messages="")
    except (KeyError, IndexError) as e:
        raise RuntimeError(
            f"Preset {path}: user template references unknown placeholder {e!s}. "
            f"Only {_PIPELINE_PLACEHOLDERS} are provided by the pipeline."
        ) from e


def _default_user_tail(language: str) -> str:
    return DEFAULT_USER_TAIL.get(language, DEFAULT_USER_TAIL["en"])


def _load_preset_file(path: Path, *, language: str = "en") -> Preset:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)

    if USER_MARKER in body:
        system, user = body.split(USER_MARKER, 1)
        system = system.strip()
        user_template = user.strip()
    else:
        system = body.strip()
        user_template = _default_user_tail(language)

    # Ensure the user template carries all placeholders the pipeline expects.
    for key in ("{period}", "{title}", "{msg_count}", "{messages}"):
        if key not in user_template:
            user_template = user_template + "\n" + key

    _validate_user_template(user_template, path=path)

    name = meta.get("name") or path.stem
    if name != path.stem:
        raise RuntimeError(
            f"Preset {path}: frontmatter name {name!r} does not match filename stem "
            f"{path.stem!r}. Rename the file or update the name field so the two match."
        )
    max_chunk_raw = meta.get("max_chunk_input_tokens")
    max_chunk_input_tokens = int(max_chunk_raw) if max_chunk_raw else None
    return Preset(
        name=name,
        prompt_version=meta.get("prompt_version", "v1"),
        system=system,
        user_template=user_template,
        needs_reduce=_coerce_bool(meta.get("needs_reduce", "true")),
        filter_model=meta.get("filter_model", "gpt-5.4-nano"),
        final_model=meta.get("final_model", "gpt-5.4"),
        output_budget_tokens=int(meta.get("output_budget_tokens", "1500")),
        map_output_tokens=int(meta.get("map_output_tokens", "1500")),
        max_chunk_input_tokens=max_chunk_input_tokens,
        description=meta.get("description") or None,
        enrich_kinds=_coerce_list(meta.get("enrich", "")),
    )


def _language_dir(language: str) -> Path:
    return PRESETS_DIR / language


# Cache: language → {preset_name: Preset}. First access for a language builds
# and stores; subsequent accesses are O(1). `clear_preset_cache()` for tests.
_presets_cache: dict[str, dict[str, Preset]] = {}


def get_presets(language: str) -> dict[str, Preset]:
    """Return all presets available in the given language directory.

    Caches per language. Raises if the language directory doesn't exist
    (use the actual filesystem, not a fallback to another language —
    each language is autonomous).
    """
    if language in _presets_cache:
        return _presets_cache[language]
    lang_dir = _language_dir(language)
    if not lang_dir.is_dir():
        raise RuntimeError(
            f"Preset directory not found for language {language!r}: {lang_dir}. "
            f"Add presets there or set [locale] language to a directory that exists."
        )
    out: dict[str, Preset] = {}
    for md in sorted(lang_dir.glob("*.md")):
        # Underscore-prefixed files are internal helpers (e.g. _base.md, _reduce.md).
        if md.stem.startswith("_") or md.stem.lower() == "readme":
            continue
        preset = _load_preset_file(md, language=language)
        out[preset.name] = preset
    _presets_cache[language] = out
    return out


def clear_preset_cache() -> None:
    """For tests — force the next get_presets() call to reload."""
    _presets_cache.clear()


def _load_reduce_prompt(language: str) -> str:
    path = _language_dir(language) / "_reduce.md"
    if not path.is_file():
        return _REDUCE_FALLBACK.get(language, _REDUCE_FALLBACK["en"])
    return path.read_text(encoding="utf-8").strip()


def _load_base_system(language: str) -> str:
    """Base rules shared by every preset (citations, reactions, media tags,
    forum context hints, anti-fabrication).

    Lives in `presets/<language>/_base.md` so non-Python contributors can
    edit it without touching code. If the file is missing, fall back to
    a minimal inline string in the matching language so the app still runs.
    """
    path = _language_dir(language) / "_base.md"
    if not path.is_file():
        return _BASE_FALLBACK.get(language, _BASE_FALLBACK["en"])
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def compose_system_prompt(
    preset_system: str,
    *,
    topic_titles: dict[int, str] | None = None,
    language: str = "en",
    source_kind: str = "chat",
) -> str:
    """Merge the shared base rules with a preset-specific system prompt.

    `language` here is the **prompt / content language** — i.e., it
    selects which `presets/<language>/` tree the loader reads. Callers
    in `pipeline.run_analysis` pass `content_language` (not the UI
    `language`) so the LLM gets a natively-language prompt while the UI
    can be in something else.

    `source_kind` ∈ {"chat", "video"} hints whether the input is a
    chat / forum stream or a video transcript. Drives the preamble's
    label ("Chat:" vs. "Video:") in `format_messages` /
    `chat_header_preamble`. The base prompt itself is source-neutral
    since v6 — no per-kind system addendum is appended here.

    Order of concatenation matters for the model:
      1. BASE — global rules (citation format, reactions, media tags,
         anti-fabrication). Loaded from `presets/<language>/_base.md`.
      2. Forum addendum — **only when `topic_titles` is non-empty**.
         Explains the `=== Topic: X ===` separators and the "don't blend
         topics" rule.
      3. Preset system — the specific task (summarize, extract links, etc.).

    Build order is `system → static_context → dynamic` (CLAUDE.md
    invariant #2) — unchanged.
    """
    _ = source_kind  # currently informational; preset bodies handle kind-specific guidance
    parts: list[str] = [_load_base_system(language)]
    if topic_titles:
        parts.append(_FORUM_CONTEXT.get(language, _FORUM_CONTEXT["en"]))
    parts.append(preset_system)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Custom preset loader (--prompt-file)
# ---------------------------------------------------------------------------


def load_custom_preset(prompt_file: Path, *, language: str = "en") -> Preset:
    """Load an ad-hoc preset from a markdown file."""
    text = prompt_file.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    default_tail = _default_user_tail(language)
    if USER_MARKER in body:
        system, user = body.split(USER_MARKER, 1)
        system = system.strip()
        user_instr = user.strip()
    elif meta:
        system = body.strip()
        user_instr = default_tail
    else:
        system = _CUSTOM_FALLBACK_SYSTEM.get(language, _CUSTOM_FALLBACK_SYSTEM["en"])
        user_instr = text.strip()

    if "{messages}" not in user_instr:
        user_instr += "\n\n" + default_tail

    for key in ("{period}", "{title}", "{msg_count}", "{messages}"):
        if key not in user_instr:
            user_instr += "\n" + key

    _validate_user_template(user_instr, path=prompt_file)

    version = meta.get("prompt_version") or "custom-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    custom_max_chunk_raw = meta.get("max_chunk_input_tokens")
    custom_max_chunk = int(custom_max_chunk_raw) if custom_max_chunk_raw else None
    return Preset(
        name=meta.get("name", "custom"),
        prompt_version=version,
        system=system,
        user_template=user_instr,
        needs_reduce=_coerce_bool(meta.get("needs_reduce", "true")),
        filter_model=meta.get("filter_model", "gpt-5.4-nano"),
        final_model=meta.get("final_model", "gpt-5.4"),
        output_budget_tokens=int(meta.get("output_budget_tokens", "1500")),
        map_output_tokens=int(meta.get("map_output_tokens", "1500")),
        max_chunk_input_tokens=custom_max_chunk,
        description=meta.get("description") or None,
        enrich_kinds=_coerce_list(meta.get("enrich", "")),
    )


# ---------------------------------------------------------------------------
# Backward-compat lazy module attributes
# ---------------------------------------------------------------------------
#
# `from atg.analyzer.prompts import PRESETS / BASE_SYSTEM / REDUCE_PROMPT`
# remains the import idiom in many call sites (commands, interactive,
# tests). Resolve these lazily via __getattr__ so they reflect the
# *current* `settings.locale.language` at access time. Each access is
# cheap (cached).


def __getattr__(name: str) -> Any:  # PEP 562
    if name == "PRESETS":
        from atg.config import get_settings

        return get_presets(get_settings().locale.language)
    if name == "BASE_SYSTEM":
        from atg.config import get_settings

        return _load_base_system(get_settings().locale.language)
    if name == "REDUCE_PROMPT":
        from atg.config import get_settings

        return _load_reduce_prompt(get_settings().locale.language)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
