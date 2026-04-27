"""`atg settings` — single interactive editor for persistent user settings.

`atg settings` opens a categorized picker that handles every supported
override (languages, models, enrichment defaults, analysis tuning).
There are no `set` / `unset` / `show` / `reset` subcommands today —
the menu shows each setting's current effective value inline and
"Reset all overrides" lives as a row inside the picker.

Each override is type-aware:
- **Language** (str from a finite list) → language picker.
- **Model** (str from `[pricing.chat]` keys) → model picker.
- **Bool** (e.g. enrich.voice) → on/off toggle.
- **Int** (e.g. analyze.high_impact_reactions) → text input + validation.

Storage: every value lives in the `app_settings` table (key, value).
`db/repo.py:_OVERRIDE_KEYS` is the allow-list; `_apply_one_override`
does the type coercion when overlays are applied to the live settings
singleton on each `open_repo`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console

from atg.config import get_settings, reset_settings
from atg.db.repo import _apply_one_override, apply_db_overrides_sync, open_repo
from atg.i18n import t as _t
from atg.i18n import tf as _tf

console = Console()


# ----------------------------- Setting registry -----------------------------


@dataclass(frozen=True)
class SettingDef:
    """One configurable override: where it lives, how to render + edit it.

    `category_key` / `label_key` / `desc_key` are i18n keys (looked up
    via `_t()` lazily on each access), so the picker re-renders correctly
    when the user flips `locale.language`. `kind` selects which editor
    function runs when the user picks the row.
    """

    key: str
    category_key: str
    kind: str  # "ui_lang" | "ui_lang_clear" | "audio_lang" | "model" | "bool" | "int"
    label_key: str
    desc_key: str

    @property
    def category(self) -> str:
        return _t(self.category_key)

    @property
    def label(self) -> str:
        return _t(self.label_key)

    @property
    def desc(self) -> str:
        return _t(self.desc_key)


_SETTINGS: tuple[SettingDef, ...] = (
    # Languages
    SettingDef(
        "locale.language",
        "settings_cat_languages",
        "ui_lang",
        "set_label_locale_language",
        "set_desc_locale_language",
    ),
    SettingDef(
        "locale.content_language",
        "settings_cat_languages",
        "ui_lang_clear",
        "set_label_locale_content_language",
        "set_desc_locale_content_language",
    ),
    SettingDef(
        "openai.audio_language",
        "settings_cat_languages",
        "audio_lang",
        "set_label_audio_language",
        "set_desc_audio_language",
    ),
    # Models
    SettingDef(
        "openai.chat_model_default",
        "settings_cat_models",
        "model",
        "set_label_chat_model",
        "set_desc_chat_model",
    ),
    SettingDef(
        "openai.filter_model_default",
        "settings_cat_models",
        "model",
        "set_label_filter_model",
        "set_desc_filter_model",
    ),
    SettingDef(
        "openai.audio_model_default",
        "settings_cat_models",
        "audio_model",
        "set_label_audio_model",
        "set_desc_audio_model",
    ),
    SettingDef(
        "enrich.vision_model",
        "settings_cat_models",
        "vision_model",
        "set_label_vision_model",
        "set_desc_vision_model",
    ),
    # Enrichment defaults
    SettingDef("enrich.voice", "settings_cat_enrich", "bool", "set_label_voice", "set_desc_enrich_default"),
    SettingDef(
        "enrich.videonote",
        "settings_cat_enrich",
        "bool",
        "set_label_videonote",
        "set_desc_enrich_default",
    ),
    SettingDef("enrich.video", "settings_cat_enrich", "bool", "set_label_video", "set_desc_enrich_default"),
    SettingDef("enrich.image", "settings_cat_enrich", "bool", "set_label_image", "set_desc_enrich_default"),
    SettingDef("enrich.doc", "settings_cat_enrich", "bool", "set_label_doc", "set_desc_enrich_default"),
    SettingDef("enrich.link", "settings_cat_enrich", "bool", "set_label_link", "set_desc_enrich_default"),
    # Analysis tuning
    SettingDef(
        "analyze.high_impact_reactions",
        "settings_cat_analyze",
        "int",
        "set_label_high_impact",
        "set_desc_high_impact",
    ),
    SettingDef(
        "analyze.dedupe_forwards",
        "settings_cat_analyze",
        "bool",
        "set_label_dedupe_forwards",
        "set_desc_dedupe_forwards",
    ),
    SettingDef(
        "analyze.min_msg_chars",
        "settings_cat_analyze",
        "int",
        "set_label_min_msg_chars",
        "set_desc_min_msg_chars",
    ),
)


_BY_KEY: dict[str, SettingDef] = {s.key: s for s in _SETTINGS}


# ----------------------------- Sentinels ----------------------------------


# Picker exit / sentinels. Distinct strings so questionary's
# "value=None == no answer" quirk can't trigger.
_SENTINEL_DONE = "__settings_done__"
_SENTINEL_RESET = "__settings_reset__"
_SENTINEL_KEEP = "__settings_keep__"
_SENTINEL_EXIT = "__settings_exit__"
_SENTINEL_CLEAR = "__settings_clear__"


# ----------------------------- Public entry --------------------------------


async def cmd_settings(
    *,
    action: str | None = None,
    key: str | None = None,
    value: str | None = None,
    yes: bool = False,
) -> None:
    """Top-level entry point. Only the interactive form remains.

    The `action` / `key` / `value` / `yes` parameters are kept on the
    signature for backwards compatibility with any older caller, but
    every non-None action now redirects to the interactive form (and
    prints a one-line note about the removed sub-commands).
    """
    if action is not None:
        console.print(f"[yellow]{_tf('settings_subaction_removed', action=action)}[/]")
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        await _interactive(repo)


# ----------------------------- Interactive panel --------------------------


async def _interactive(repo) -> None:
    """Categorized menu loop.

    Every iteration:
      1. Re-read overrides + live settings (so just-saved values render).
      2. Show the menu — categories with their rows + global actions.
      3. User picks one → dispatch to the type-specific editor.
      4. After the editor returns, loop back to the menu.

    Exits cleanly on ESC / "✓ Done" / Ctrl-C.
    """
    try:
        import questionary  # noqa: F401
    except ImportError:
        console.print(f"[red]{_t('settings_no_questionary')}[/]")
        return

    console.print(f"[bold cyan]{_t('settings_banner')}[/] [dim]{_t('settings_banner_hint')}[/]")

    db_path = get_settings().storage.data_path

    def _reload_singleton() -> None:
        """Drop the cached settings + re-overlay overrides from `repo`.

        Used after any DB mutation (set / delete / clear) so the next loop
        iteration reads `s.locale.language` etc. correctly. Without this
        the value column would lag behind the DB and the picker would
        keep starring stale codes.
        """
        reset_settings()
        apply_db_overrides_sync(get_settings(), db_path)

    saved_anything = False
    while True:
        overrides = await repo.get_all_app_settings()
        s = get_settings()
        choice = await _pick_setting_to_edit(overrides, s)

        if choice is None or choice == _SENTINEL_DONE:
            break
        if choice == _SENTINEL_RESET:
            existing = await repo.get_all_app_settings()
            if not existing:
                console.print(f"[dim]{_t('settings_nothing_to_reset')}[/]")
                continue
            confirmed = await _confirm_reset(len(existing))
            if confirmed:
                n = await repo.clear_all_app_settings()
                console.print(f"[green]{_tf('settings_cleared_n', n=n)}[/]")
                _reload_singleton()
                saved_anything = True
            continue

        # Per-setting editor.
        sdef = _BY_KEY.get(choice)
        if sdef is None:
            continue
        new_value = await _edit_one(sdef, overrides, s)
        if new_value is _SENTINEL_EXIT:
            break
        if new_value is None:
            # User kept current — no-op.
            continue
        if new_value == _SENTINEL_CLEAR:
            removed = await repo.delete_app_setting(sdef.key)
            if removed:
                console.print(_tf("settings_cleared_key", key=f"[bold]{sdef.key}[/]"))
                _reload_singleton()
                saved_anything = True
            continue
        await repo.set_app_setting(sdef.key, new_value)
        # Apply the new value onto the live singleton in-place so the
        # menu's value column refreshes immediately. Same coercion logic
        # the bootstrap path uses, so the in-session view matches what the
        # next process would see.
        _apply_one_override(get_settings(), sdef.key, new_value)
        display_value = new_value or _t("settings_empty_value")
        console.print(_tf("settings_saved_kv", key=f"[bold]{sdef.key}[/]", value=f"[cyan]{display_value}[/]"))
        saved_anything = True

    if saved_anything:
        console.print(f"\n[green]{_t('settings_done_with_changes')}[/]")
    else:
        console.print(f"\n[dim]{_t('settings_done_no_changes')}[/]")
    # Refresh the in-process singleton so a follow-up call in the same
    # shell session picks up new values.
    reset_settings()


# ----------------------------- Menu picker --------------------------------


async def _pick_setting_to_edit(overrides: dict[str, str], s: Any) -> str | None:
    """Build the categorized picker; return the selected sentinel/key."""
    import questionary

    from atg.interactive import LIST_STYLE, _bind_escape

    # Compute display widths once so values + descriptions line up.
    label_w = max(len(sd.label) for sd in _SETTINGS)
    val_w = 0
    rows: list[tuple[SettingDef, str, bool]] = []
    for sd in _SETTINGS:
        cur = _current_display(sd, overrides, s)
        rows.append((sd, cur, sd.key in overrides))
        val_w = max(val_w, len(cur))
    # Cap value column so a long custom model name doesn't bulldoze the
    # description off-screen in narrow terminals.
    val_w = min(val_w, 36)

    items: list = []
    cur_category: str | None = None
    for sd, cur, has_override in rows:
        if sd.category != cur_category:
            cur_category = sd.category
            items.append(questionary.Separator(f"── {cur_category} ──"))
        marker = " ★" if has_override else "  "
        cur_clipped = cur if len(cur) <= val_w else cur[: val_w - 1] + "…"
        # questionary doesn't render Rich markup in choice titles —
        # `[dim]…[/dim]` would print literally. Use plain whitespace.
        items.append(
            questionary.Choice(
                title=f"{sd.label:<{label_w}}  {cur_clipped:<{val_w}}{marker}    {sd.desc}",
                value=sd.key,
            )
        )
    items.append(questionary.Separator())
    items.append(questionary.Choice(title=_t("settings_reset_row"), value=_SENTINEL_RESET))
    items.append(questionary.Separator())
    items.append(questionary.Choice(title=_t("settings_done_row"), value=_SENTINEL_DONE))

    picked = await _bind_escape(
        questionary.select(
            _t("settings_pick_prompt"),
            choices=items,
            use_search_filter=True,
            use_jk_keys=False,
            instruction=_t("wiz_filter_instruction"),
            style=LIST_STYLE,
        ),
        _SENTINEL_DONE,
    ).ask_async()
    return picked


def _current_display(sd: SettingDef, overrides: dict[str, str], s: Any) -> str:
    """Return the current effective value of `sd` as a display string."""
    if sd.kind == "ui_lang":
        return str(s.locale.language or "en")
    if sd.kind == "ui_lang_clear":
        return str(s.locale.content_language or _t("settings_value_follows_ui"))
    if sd.kind == "audio_lang":
        return str(s.openai.audio_language or _t("settings_value_autodetect"))
    if sd.kind in {"model", "audio_model", "vision_model"}:
        section, attr = sd.key.split(".", 1)
        return str(getattr(getattr(s, section), attr) or _t("settings_value_unset"))
    if sd.kind == "bool":
        section, attr = sd.key.split(".", 1)
        return _t("settings_value_on") if getattr(getattr(s, section), attr) else _t("settings_value_off")
    if sd.kind == "int":
        section, attr = sd.key.split(".", 1)
        return str(getattr(getattr(s, section), attr))
    return overrides.get(sd.key, "")


# ----------------------------- Per-type editors --------------------------


async def _edit_one(sd: SettingDef, overrides: dict[str, str], s: Any) -> str | None:
    """Dispatch to the appropriate editor; return the new value (str) or
    a sentinel (`_SENTINEL_KEEP` → no change, `_SENTINEL_CLEAR` → drop
    override, `_SENTINEL_EXIT` → exit settings)."""
    if sd.kind == "ui_lang":
        cur = overrides.get(sd.key) or s.locale.language or "en"
        return await _pick_language(sd, cur, allow_clear=False)
    if sd.kind == "ui_lang_clear":
        cur = overrides.get(sd.key) or ""
        return await _pick_language(sd, cur, allow_clear=True, clear_label=_t("settings_clear_follow_ui"))
    if sd.kind == "audio_lang":
        cur = overrides.get(sd.key) or ""
        return await _pick_language(
            sd, cur, allow_clear=True, clear_label=_t("settings_clear_autodetect"), audio_pool=True
        )
    if sd.kind == "model":
        cur = overrides.get(sd.key) or _read_attr(s, sd.key)
        return await _pick_model(sd, cur, kind="chat")
    if sd.kind == "audio_model":
        cur = overrides.get(sd.key) or _read_attr(s, sd.key)
        return await _pick_model(sd, cur, kind="audio")
    if sd.kind == "vision_model":
        cur = overrides.get(sd.key) or _read_attr(s, sd.key)
        return await _pick_model(sd, cur, kind="chat")
    if sd.kind == "bool":
        cur_bool = bool(_read_attr(s, sd.key))
        return await _pick_bool(sd, cur_bool)
    if sd.kind == "int":
        cur_int = int(_read_attr(s, sd.key))
        return await _pick_int(sd, cur_int)
    return None


def _read_attr(s: Any, key: str) -> Any:
    section, attr = key.split(".", 1)
    return getattr(getattr(s, section), attr)


async def _pick_language(
    sd: SettingDef,
    current: str,
    *,
    allow_clear: bool,
    clear_label: str = "",
    audio_pool: bool = False,
) -> str | None:
    import questionary

    from atg.i18n import LANGUAGE_NAMES
    from atg.interactive import LIST_STYLE, _bind_escape

    pool = _supported_audio_languages() if audio_pool else _supported_locale_languages()
    if current and current not in pool:
        pool = [current, *pool]

    items: list = []
    for code in pool:
        name = LANGUAGE_NAMES.get(code, code.title())
        marker = "  ★" if code == current else ""
        items.append(questionary.Choice(title=f"{code:<4} — {name}{marker}", value=code))
    if allow_clear:
        items.append(questionary.Separator())
        items.append(questionary.Choice(title=clear_label, value=_SENTINEL_CLEAR))
    items.append(questionary.Separator())
    items.append(questionary.Choice(title=_t("settings_keep_current"), value=_SENTINEL_KEEP))
    items.append(questionary.Choice(title=_t("settings_exit_row"), value=_SENTINEL_EXIT))

    default = current if current in pool else _SENTINEL_KEEP
    # questionary doesn't render Rich markup; print the desc on a separate
    # line via console.print() then ask a plain prompt.
    console.print(f"[dim]{sd.desc}[/dim]")
    picked = await _bind_escape(
        questionary.select(
            sd.label,
            choices=items,
            default=default,
            use_search_filter=True,
            use_jk_keys=False,
            instruction=_t("wiz_filter_instruction"),
            style=LIST_STYLE,
        ),
        _SENTINEL_KEEP,
    ).ask_async()
    if picked is None:
        return _SENTINEL_EXIT
    if picked == _SENTINEL_KEEP:
        return None
    if picked == _SENTINEL_CLEAR:
        return _SENTINEL_CLEAR
    if picked == _SENTINEL_EXIT:
        return _SENTINEL_EXIT
    if picked == current:
        return None
    return picked


async def _pick_model(sd: SettingDef, current: str, *, kind: str) -> str | None:
    """Pick a model name from the configured pricing table.

    Pricing table = the source of truth for "what models the user has
    cost data for". Picking a model outside the table runs fine but
    `--max-cost` won't enforce, and `atg doctor` warns. We expose the
    table's keys + a "Custom…" text-input for power users.
    """
    import questionary

    from atg.config import get_settings as _get_settings
    from atg.interactive import LIST_STYLE, _bind_escape

    s = _get_settings()
    pool: list[str]
    if kind == "audio":
        pool = sorted((s.pricing.audio or {}).keys())
    else:
        pool = sorted((s.pricing.chat or {}).keys())
    if not pool:
        console.print(f"[yellow]{_t('settings_no_pricing_models')}[/]")

    items: list = []
    for name in pool:
        marker = "  ★" if name == current else ""
        items.append(questionary.Choice(title=f"{name}{marker}", value=name))
    if pool:
        items.append(questionary.Separator())
    items.append(questionary.Choice(title=_t("settings_custom_model_row"), value="__custom__"))
    items.append(questionary.Separator())
    items.append(questionary.Choice(title=_t("settings_keep_current"), value=_SENTINEL_KEEP))
    items.append(questionary.Choice(title=_t("settings_exit_row"), value=_SENTINEL_EXIT))

    default = current if current in pool else _SENTINEL_KEEP
    console.print(f"[dim]{sd.desc} — current: {current}[/dim]")
    picked = await _bind_escape(
        questionary.select(
            sd.label,
            choices=items,
            default=default,
            use_search_filter=True,
            use_jk_keys=False,
            instruction=_t("wiz_filter_instruction"),
            style=LIST_STYLE,
        ),
        _SENTINEL_KEEP,
    ).ask_async()
    if picked is None or picked == _SENTINEL_KEEP:
        return None
    if picked == _SENTINEL_EXIT:
        return _SENTINEL_EXIT
    if picked == "__custom__":
        raw = await questionary.text(_tf("settings_custom_model_prompt", key=sd.key), default="").ask_async()
        if not raw:
            return None
        return raw.strip()
    if picked == current:
        return None
    return picked


async def _pick_bool(sd: SettingDef, current: bool) -> str | None:
    """Render a 3-option toggle: On / Off / Keep current."""
    import questionary

    from atg.interactive import LIST_STYLE, _bind_escape

    on_label = _t("settings_state_on")
    off_label = _t("settings_state_off")
    items = [
        questionary.Choice(title=f"{on_label}{'  ★' if current else ''}", value="1"),
        questionary.Choice(title=f"{off_label}{'  ★' if not current else ''}", value="0"),
        questionary.Separator(),
        questionary.Choice(title=_t("settings_keep_current"), value=_SENTINEL_KEEP),
        questionary.Choice(title=_t("settings_exit_row"), value=_SENTINEL_EXIT),
    ]
    console.print(f"[dim]{sd.desc}[/dim]")
    picked = await _bind_escape(
        questionary.select(
            sd.label,
            choices=items,
            default="1" if current else "0",
            style=LIST_STYLE,
        ),
        _SENTINEL_KEEP,
    ).ask_async()
    if picked is None or picked == _SENTINEL_KEEP:
        return None
    if picked == _SENTINEL_EXIT:
        return _SENTINEL_EXIT
    new_bool = picked == "1"
    if new_bool == current:
        return None
    return picked


async def _pick_int(sd: SettingDef, current: int) -> str | None:
    """Free-text integer input with validation. Re-prompts on garbage."""
    import questionary

    while True:
        raw = await questionary.text(
            _tf("settings_int_prompt", label=sd.label, desc=sd.desc, current=current),
            default="",
        ).ask_async()
        if raw is None:
            return _SENTINEL_EXIT
        raw = raw.strip()
        if not raw:
            return None
        if raw.lower() in {"q", "quit", "exit"}:
            return _SENTINEL_EXIT
        try:
            new_int = int(raw)
        except ValueError:
            console.print(f"[red]{_tf('settings_not_an_integer', raw=raw)}[/]")
            continue
        if new_int < 0:
            console.print(f"[red]{_t('settings_must_be_nonneg')}[/]")
            continue
        if new_int == current:
            return None
        return str(new_int)


# ----------------------------- Reset --------------------------


async def _confirm_reset(n: int) -> bool:
    """Modal confirm before wiping every override."""
    prompt = _tf("settings_drop_n_q", n=n)
    try:
        import questionary

        return bool(await questionary.confirm(prompt, default=False).ask_async())
    except ImportError:
        return typer.confirm(prompt, default=False)


# ----------------------------- Language pools ------------------------------


def _supported_locale_languages() -> list[str]:
    """ISO codes that ship both a preset tree AND i18n entries."""
    from atg.analyzer.prompts import PRESETS_DIR
    from atg.i18n import _STRINGS

    i18n_langs: set[str] = set()
    for entries in _STRINGS.values():
        i18n_langs.update(entries.keys())
    preset_langs: set[str] = set()
    if PRESETS_DIR.is_dir():
        for child in PRESETS_DIR.iterdir():
            if not child.is_dir():
                continue
            if (child / "_base.md").is_file() and (child / "_reduce.md").is_file():
                preset_langs.add(child.name)
    supported = i18n_langs & preset_langs
    others = sorted(c for c in supported if c != "en")
    return (["en"] if "en" in supported else []) + others


def _supported_audio_languages() -> list[str]:
    """Whisper hint pool: UI-supported languages first, then common spoken ones."""
    common = ("en", "ru", "de", "fr", "es", "it", "pt", "uk", "pl", "tr", "zh", "ja", "ko", "ar")
    ui = _supported_locale_languages()
    seen = set(ui)
    extras = [c for c in common if c not in seen]
    return ui + extras
