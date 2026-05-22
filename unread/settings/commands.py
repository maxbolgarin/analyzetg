"""`unread settings` — single interactive editor for persistent user settings.

`unread settings` opens a categorized picker that handles every supported
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

from rich.console import Console

from unread.config import get_settings, reset_settings
from unread.db.repo import _apply_one_override, apply_db_overrides_sync, open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf

console = Console()


def _compose_instruction(desc: str, current: str | None = None, prefix: str = "") -> str:
    """Build the dim instruction string under a picker prompt.

    Combines a per-setting description (and optionally the current value /
    a one-line prefix like "Models available for provider: anthropic")
    with the default key-help line, so prompt_toolkit owns the whole
    helper region and `erase_when_done=True` wipes it cleanly when the
    user picks. Keeps callers from `console.print`'ing residue lines that
    would otherwise pile up in scrollback.
    """
    from unread.util.prompt import _default_select_instruction

    body = desc
    if current is not None:
        body = f"{desc} — current: {current or '(unset)'}"
    if prefix:
        body = f"{prefix}\n{body}"
    return f"{body}\n{_default_select_instruction()}"


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


# Top-level settings — shown on the main `unread settings` menu.
# Order: Languages → Models (4 compound (provider, model) rows) →
# API keys. The Tuning sub-page (`_TUNING_SETTINGS`) holds enrichment
# defaults + analyze tuning so the top page stays focused.
#
# Per-slot routing landed in 2026-05: each model row writes BOTH
# `ai.<slot>_provider` AND `ai.<slot>_model`, replacing the umbrella
# `ai.provider`. The legacy `ai.provider` row is gone — the bootstrap
# migration in `db.repo._migrate_legacy_ai_provider_sync` rewrites old
# rows automatically. Each model row's `key` carries the slot name in a
# `__slot_<slot>__` synthetic form so the dispatcher can locate it
# while keeping the on-disk override key parsing unambiguous.
_TOP_SETTINGS: tuple[SettingDef, ...] = (
    # Languages — three independent axes; see :class:`unread.config.LocaleCfg`.
    SettingDef(
        "locale.language",
        "settings_cat_languages",
        "ui_lang",
        "set_label_locale_language",
        "set_desc_locale_language",
    ),
    SettingDef(
        "locale.report_language",
        "settings_cat_languages",
        "ui_lang_clear",
        "set_label_locale_report_language",
        "set_desc_locale_report_language",
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
    # Models — four compound (provider, model) rows. The `kind`
    # `slot_model` dispatches to `_pick_provider_and_model` which runs
    # a two-step picker (provider → model from that provider's
    # role-filtered catalog) and persists both keys atomically.
    SettingDef(
        "__slot_chat__",
        "settings_cat_models",
        "slot_model",
        "set_label_ai_chat_model",
        "set_desc_ai_chat_model",
    ),
    SettingDef(
        "__slot_filter__",
        "settings_cat_models",
        "slot_model",
        "set_label_ai_filter_model",
        "set_desc_ai_filter_model",
    ),
    SettingDef(
        "__slot_audio__",
        "settings_cat_models",
        "slot_model",
        "set_label_audio_model",
        "set_desc_audio_model",
    ),
    SettingDef(
        "__slot_vision__",
        "settings_cat_models",
        "slot_model",
        "set_label_vision_model",
        "set_desc_vision_model",
    ),
    # API keys — one row per keyed provider plus the local server URL.
    # Each row dispatches to `_manage_provider_key(<provider>)` or
    # to `_edit_local_base_url(repo)`. Synthetic key prefix `__api_key:`
    # carries the provider name; the dispatcher parses it.
    SettingDef(
        "__api_key:openai__",
        "settings_cat_api_keys",
        "api_key",
        "set_label_api_key_openai",
        "set_desc_api_key_openai",
    ),
    SettingDef(
        "__api_key:openrouter__",
        "settings_cat_api_keys",
        "api_key",
        "set_label_api_key_openrouter",
        "set_desc_api_key_openrouter",
    ),
    SettingDef(
        "__api_key:anthropic__",
        "settings_cat_api_keys",
        "api_key",
        "set_label_api_key_anthropic",
        "set_desc_api_key_anthropic",
    ),
    SettingDef(
        "__api_key:google__",
        "settings_cat_api_keys",
        "api_key",
        "set_label_api_key_google",
        "set_desc_api_key_google",
    ),
    SettingDef(
        "__api_key:local__",
        "settings_cat_api_keys",
        "api_key",
        "set_label_api_key_local",
        "set_desc_api_key_local",
    ),
)


# Tuning sub-page — opened via the `⚙ Tuning…` row on the main menu.
# Holds enrichment defaults + analyze tuning. Per-slot model routing
# moved to the top-level Models section; `ai.base_url` and the legacy
# `openai.<chat|filter>_model_default` keys are no longer surfaced
# here (still readable from `config.toml` / `.env` for power users).
_TUNING_SETTINGS: tuple[SettingDef, ...] = (
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
    SettingDef(
        "analyze.plain_citations",
        "settings_cat_analyze",
        "bool",
        "set_label_plain_citations",
        "set_desc_plain_citations",
    ),
    SettingDef(
        "analyze.no_citations",
        "settings_cat_analyze",
        "bool",
        "set_label_no_citations",
        "set_desc_no_citations",
    ),
    # Output verbosity — `silent` / `normal` / `verbose` / `debug`.
    # Editor is a 4-way enum picker; persisted as `logging.mode` in
    # `app_settings` (and read at every `setup_logging` call via the
    # `resolve_log_mode` precedence chain).
    SettingDef(
        "logging.mode",
        "settings_cat_output",
        "log_mode",
        "set_label_log_mode",
        "set_desc_log_mode",
    ),
)


# Per-slot capability filter for the provider half of the compound
# picker. Audio is restricted to providers with an SDK-compatible
# Whisper-shape API (see `unread.ai.providers._AUDIO_PROVIDERS` for
# why openrouter is excluded — its endpoint rejects multipart). Chat /
# filter / vision accept all five providers.
_SLOT_PROVIDERS: dict[str, tuple[str, ...]] = {
    "chat": ("openai", "openrouter", "anthropic", "google", "local"),
    "filter": ("openai", "openrouter", "anthropic", "google", "local"),
    "audio": ("openai", "local"),
    "vision": ("openai", "openrouter", "anthropic", "google", "local"),
}

_SLOT_ROLE: dict[str, str] = {
    "chat": "chat",
    "filter": "filter",
    "audio": "audio",
    "vision": "vision",
}


# Combined registry — every setting must be reachable via direct key
# lookup so the editor still finds existing override rows after a
# regrouping (the on-disk override key never changes).
_SETTINGS: tuple[SettingDef, ...] = _TOP_SETTINGS + _TUNING_SETTINGS


_BY_KEY: dict[str, SettingDef] = {s.key: s for s in _SETTINGS}


# Provider → secret-table key mapping. `local` is intentionally absent —
# it doesn't use an API key (the SDK accepts a placeholder and most
# self-hosted servers don't enforce one). Used by the inline key
# editor that runs after the provider picker.
_PROVIDER_SECRET_KEYS: dict[str, str] = {
    "openai": "openai.api_key",
    "openrouter": "openrouter.api_key",
    "anthropic": "anthropic.api_key",
    "google": "google.api_key",
}

# Where to grab a fresh key for each provider — surfaced in the inline
# editor so the user doesn't have to switch tabs to recall the URL.
_PROVIDER_KEY_URLS: dict[str, str] = {
    "openai": "https://platform.openai.com/api-keys",
    "openrouter": "https://openrouter.ai/keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "google": "https://aistudio.google.com/app/apikey",
}


def _visible_settings(
    active_provider: str, pool: tuple[SettingDef, ...] = _SETTINGS
) -> tuple[SettingDef, ...]:
    """Filter `pool` to settings that actually take effect right now.

    With the per-slot provider routing landed in 2026-05, the legacy
    "hide rows that only affect OpenAI" filter is no longer needed —
    every model row is provider-aware via the slot it represents.
    The argument signature is kept for API stability; `active_provider`
    is unused today.
    """
    del active_provider  # kept for API compat; no longer drives filtering
    return pool


# ----------------------------- Sentinels ----------------------------------


# Picker exit / sentinels. Distinct strings so questionary's
# "value=None == no answer" quirk can't trigger.
_SENTINEL_DONE = "__settings_done__"
_SENTINEL_RESET = "__settings_reset__"
_SENTINEL_KEEP = "__settings_keep__"
_SENTINEL_EXIT = "__settings_exit__"
_SENTINEL_CLEAR = "__settings_clear__"
_SENTINEL_TUNING = "__settings_tuning__"
_SENTINEL_BACK = "__settings_back__"
# Returned by the API-key editor when the secret was mutated. Tells the
# main loop "no app_settings write needed, but bump saved_anything so
# the final summary line reflects the change."
_SENTINEL_KEY_SAVED = "__settings_key_saved__"


def _reload_settings_singleton() -> None:
    """Drop the cached settings singleton and re-overlay DB overrides.

    Used after any DB mutation (override write, secret put/delete) so
    later reads see the new values immediately. Captures the install
    DB path before the reset since `get_settings()` afterwards returns
    a brand-new instance.
    """
    db_path = get_settings().storage.data_path
    reset_settings()
    apply_db_overrides_sync(get_settings(), db_path)


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
    """Top-level entry into the categorized settings editor.

    The shared loop body lives in :func:`_run_menu_loop` — it's
    parameterized by `pool` (which `_SETTINGS` subset to render) and
    `mode` (which action rows to show: `top` exposes Tuning + Reset +
    Done; `tuning` exposes ← Back). The Tuning button recurses into the
    same loop with `_TUNING_SETTINGS`; everything else stays exactly as
    it was — same editor logic, same advisories, same singleton refresh.
    """
    try:
        import questionary  # noqa: F401
    except ImportError:
        console.print(f"[red]{_t('settings_no_questionary')}[/]")
        return

    console.print(f"[bold cyan]{_t('settings_banner')}[/] [grey70]{_t('settings_banner_hint')}[/]")

    # No in-place redraw: every picker call passes `erase=True` so its
    # rendered region (header + choices + instruction) is wiped on exit
    # by prompt_toolkit's `erase_when_done`. Intentional status messages
    # (`settings_cleared_n`, `lang_axes_hint`, the HTTPS warning, …)
    # still print and persist in scrollback as a record of what changed.

    saved_anything = await _run_menu_loop(repo, pool=_TOP_SETTINGS, mode="top")

    if saved_anything:
        console.print(f"[green]{_t('settings_done_with_changes')}[/]")
    else:
        console.print(f"[grey70]{_t('settings_done_no_changes')}[/]")
    # Refresh the in-process singleton so a follow-up call in the same
    # shell session picks up new values.
    reset_settings()


async def _run_menu_loop(repo, *, pool: tuple[SettingDef, ...], mode: str) -> bool:
    """Shared menu loop. Returns True iff anything was saved.

    Every iteration:
      1. Re-read overrides + live settings (so just-saved values render).
      2. Show the menu — `pool`'s rows grouped by category, plus the
         action rows appropriate for `mode`.
      3. Dispatch the choice (action row or per-setting editor).

    `mode="top"` shows ⚙ Tuning…, ♻ Reset, ✓ Done.
    `mode="tuning"` shows ← Back. Reset always lives on the top page so
    a sub-page accident can't wipe every override.
    """
    saved_anything = False
    while True:
        overrides = await repo.get_all_app_settings()
        s = get_settings()
        try:
            choice = await _pick_setting_to_edit(overrides, s, pool=pool, mode=mode)
        except KeyboardInterrupt:
            break

        if choice is None or choice == _SENTINEL_DONE:
            break
        if choice == _SENTINEL_BACK:
            break
        if choice == _SENTINEL_TUNING:
            sub_saved = await _run_menu_loop(repo, pool=_TUNING_SETTINGS, mode="tuning")
            saved_anything = saved_anything or sub_saved
            continue
        if choice == _SENTINEL_RESET:
            existing = await repo.get_all_app_settings()
            if not existing:
                console.print(f"[grey70]{_t('settings_nothing_to_reset')}[/]")
                continue
            confirmed = await _confirm_reset(len(existing))
            if confirmed:
                n = await repo.clear_all_app_settings()
                console.print(f"[green]{_tf('settings_cleared_n', n=n)}[/]")
                _reload_settings_singleton()
                saved_anything = True
            continue

        # Per-setting editor.
        sdef = _BY_KEY.get(choice)
        if sdef is None:
            continue
        new_value = await _edit_one(sdef, overrides, s, repo=repo)
        if new_value is _SENTINEL_EXIT:
            break
        if new_value is None:
            # User kept current — no-op.
            continue
        if new_value == _SENTINEL_KEY_SAVED:
            # `_manage_provider_key` already wrote to the secrets table
            # and refreshed the singleton; just record that something
            # changed so the final summary line reflects it.
            saved_anything = True
            continue
        if new_value == _SENTINEL_CLEAR:
            removed = await repo.delete_app_setting(sdef.key)
            if removed:
                console.print(_tf("settings_cleared_key", key=f"[bold]{sdef.key}[/]"))
                _reload_settings_singleton()
                saved_anything = True
            continue
        # Real settings — write through to `app_settings` and overlay
        # the live singleton for immediate redraw.
        await repo.set_app_setting(sdef.key, new_value)
        _apply_one_override(get_settings(), sdef.key, new_value)
        # Advisory: when the user changes UI language but report_language
        # is empty/unset, remind them that LLM output language is a
        # separate axis (and content_language is yet another, source-hint).
        if sdef.key == "locale.language" and new_value:
            report_lang = (overrides.get("locale.report_language") or "").strip()
            if not report_lang:
                console.print(f"[grey70]{_tf('lang_axes_hint', lang=new_value)}[/]")
        # Apply the new log mode for the rest of this settings session so
        # the user sees silent-mode quiet immediately, not just after exit.
        if sdef.key == "logging.mode" and new_value:
            from unread.util.logging import setup_logging

            setup_logging(mode=new_value)
        saved_anything = True
    return saved_anything


# ----------------------------- Menu picker --------------------------------


async def _pick_setting_to_edit(
    overrides: dict[str, str],
    s: Any,
    *,
    pool: tuple[SettingDef, ...],
    mode: str,
) -> str | None:
    """Build the categorized picker; return the selected sentinel/key.

    `pool` is the subset of settings to render (top-level vs. tuning);
    `mode` decides which action rows appear at the bottom:
    `top` → ⚙ Tuning…, ♻ Reset, ✓ Done. `tuning` → ← Back.
    """
    from unread.util.prompt import Choice
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    # Visible-only set: hides provider-scoped rows whose provider isn't
    # active. The full _SETTINGS list still drives _BY_KEY so any
    # already-set override stays editable via direct key lookup.
    active_provider = str(getattr(s.ai, "provider", "openai") or "openai")
    visible = _visible_settings(active_provider, pool)
    # Compute display widths once so values + descriptions line up.
    # Labels can morph contextually (e.g. provider_key → "Local server URL"
    # under the local provider), so width comes from the rendered labels.
    val_w = 0
    rows: list[tuple[SettingDef, str, str, str, bool]] = []
    for sd in visible:
        cur = _current_display(sd, overrides, s)
        label, desc = _row_label_desc(sd, active_provider)
        rows.append((sd, cur, label, desc, _has_user_value(sd, overrides, s)))
        val_w = max(val_w, len(cur))
    label_w = max(len(label) for _, _, label, _, _ in rows)
    # Cap value column at a fairly generous width so the compound
    # `<provider> | <model>  (default)` strings (up to ~55 chars for
    # OpenRouter aliases like `openrouter | openai/gpt-4o-mini-transcribe`)
    # render in full. The description column wraps to the next line on
    # narrow terminals — better than silently truncating the model name.
    val_w = min(val_w, 64)

    items: list = []
    cur_category: str | None = None
    for sd, cur, label, desc, has_override in rows:
        if sd.category != cur_category:
            cur_category = sd.category
            items.append(_sep(f"── {cur_category} ──"))
        marker = " ★" if has_override else "  "
        cur_clipped = cur if len(cur) <= val_w else cur[: val_w - 1] + "…"
        items.append(
            Choice(
                value=sd.key,
                label=f"{label:<{label_w}}  {cur_clipped:<{val_w}}{marker}    {desc}",
            )
        )
    items.append(_sep())
    if mode == "top":
        items.append(Choice(value=_SENTINEL_TUNING, label=_t("settings_tuning_row")))
        items.append(_sep())
        items.append(Choice(value=_SENTINEL_RESET, label=_t("settings_reset_row")))
        items.append(_sep())
        items.append(Choice(value=_SENTINEL_DONE, label=_t("settings_done_row")))
    else:
        items.append(Choice(value=_SENTINEL_BACK, label=_t("settings_back_row")))

    prompt = _t("settings_tuning_prompt") if mode == "tuning" else _t("settings_pick_prompt")
    picked = _select(prompt, choices=items, erase=True)
    if picked is None:
        # ESC — exit fully from the top page, return to top from a sub-page.
        return _SENTINEL_BACK if mode == "tuning" else _SENTINEL_DONE
    return picked


def _slot_from_key(key: str) -> str:
    """Extract the slot name from a synthetic `__slot_<slot>__` key."""
    if key.startswith("__slot_") and key.endswith("__"):
        return key[len("__slot_") : -len("__")]
    return ""


def _provider_from_api_key_row(key: str) -> str:
    """Extract the provider name from a synthetic `__api_key:<provider>__` row."""
    if key.startswith("__api_key:") and key.endswith("__"):
        return key[len("__api_key:") : -len("__")]
    return ""


def _row_label_desc(sd: SettingDef, active_provider: str) -> tuple[str, str]:
    """Return the label + desc for `sd`. Today no row morphs contextually.

    Kept as a hook in case a future row needs provider-aware labeling.
    The `active_provider` argument is unused today.
    """
    del active_provider
    return sd.label, sd.desc


def _has_user_value(sd: SettingDef, overrides: dict[str, str], s: Any) -> bool:
    """True if the row's value is "user-set" (drives the ★ marker).

    Real settings: presence in the `app_settings` overrides dict.
    Synthetic rows have row-specific logic: a `slot_model` row counts as
    "user-set" when either the slot's `_provider` or `_model` override
    is present; an `api_key` row counts as set when the provider has a
    non-empty key (or for `local`, when `local.base_url` is overridden).
    """
    if sd.kind == "slot_model":
        slot = _slot_from_key(sd.key)
        return f"ai.{slot}_provider" in overrides or f"ai.{slot}_model" in overrides
    if sd.kind == "api_key":
        provider = _provider_from_api_key_row(sd.key)
        if provider == "local":
            return "local.base_url" in overrides
        return bool(_provider_api_key(s, provider))
    return sd.key in overrides


def _current_display(sd: SettingDef, overrides: dict[str, str], s: Any) -> str:
    """Return the current effective value of `sd` as a display string."""
    if sd.kind == "ui_lang":
        return str(s.locale.language or "en")
    if sd.kind == "ui_lang_clear":
        # Two rows share this widget — locale.report_language (empty
        # falls back to UI) and locale.content_language (empty means
        # "let the LLM auto-detect from the source"). Display the right
        # value AND the right "empty" placeholder for each.
        section, attr = sd.key.split(".", 1)
        cur = getattr(getattr(s, section), attr, "") or ""
        if cur:
            return str(cur)
        if sd.key == "locale.content_language":
            return _t("settings_value_autodetect")
        return _t("settings_value_follows_ui")
    if sd.kind == "audio_lang":
        return str(s.openai.audio_language or _t("settings_value_autodetect"))
    if sd.kind == "slot_model":
        # Compound (provider, model) display: `<provider>/<model>` with
        # a "(default)" suffix when the model half wasn't pinned (the
        # resolver's class default is showing). The provider half always
        # has a value — defaults to the resolver's "openai" fallback.
        slot = _slot_from_key(sd.key)
        from unread.ai.providers import resolve_audio, resolve_chat, resolve_filter, resolve_vision

        resolver = {
            "chat": resolve_chat,
            "filter": resolve_filter,
            "audio": resolve_audio,
            "vision": resolve_vision,
        }.get(slot)
        if resolver is None:
            return _t("settings_value_unset")
        provider, model = resolver(s)
        explicit_model = getattr(s.ai, f"{slot}_model", "") or ""
        rendered = f"{provider} | {model}"
        if not explicit_model:
            return _tf("settings_value_default", value=rendered)
        return rendered
    if sd.kind == "api_key":
        # `__api_key:<provider>__`: masked key for the four key-bearing
        # providers, base URL for local. Empty key → "(unset)".
        provider = _provider_from_api_key_row(sd.key)
        if provider == "local":
            return str(s.local.base_url or _t("settings_value_unset"))
        existing = _provider_api_key(s, provider)
        return _mask_secret(existing) if existing else _t("settings_value_unset")
    if sd.kind == "bool":
        section, attr = sd.key.split(".", 1)
        return _t("settings_value_on") if getattr(getattr(s, section), attr) else _t("settings_value_off")
    if sd.kind == "int":
        section, attr = sd.key.split(".", 1)
        return str(getattr(getattr(s, section), attr))
    if sd.kind == "log_mode":
        # The persisted enum string itself — same vocab the user reads
        # on the picker, so what they pick is what they see in the row.
        return str(s.logging.mode or "normal")
    if sd.kind in {"provider", "string"}:
        section, attr = sd.key.split(".", 1)
        cur = getattr(getattr(s, section), attr)
        return str(cur) if cur else _t("settings_value_unset")
    return overrides.get(sd.key, "")


# ----------------------------- Per-type editors --------------------------


async def _edit_one(sd: SettingDef, overrides: dict[str, str], s: Any, *, repo) -> str | None:
    """Dispatch to the appropriate editor; return the new value (str) or
    a sentinel (`_SENTINEL_KEEP` → no change, `_SENTINEL_CLEAR` → drop
    override, `_SENTINEL_EXIT` → exit settings).

    `repo` is threaded through so the provider editor can persist the
    selected provider's API key inline (set / update / delete) without
    juggling its own DB connection.
    """
    if sd.kind == "ui_lang":
        cur = overrides.get(sd.key) or s.locale.language or "en"
        return await _pick_language(sd, cur, allow_clear=False, strict_ui=True)
    if sd.kind == "ui_lang_clear":
        cur = overrides.get(sd.key) or ""
        # The two rows have different "clear" semantics: report_language
        # follows the UI; content_language switches to LLM auto-detect.
        clear_label = (
            _t("settings_clear_autodetect")
            if sd.key == "locale.content_language"
            else _t("settings_clear_follow_ui")
        )
        return await _pick_language(sd, cur, allow_clear=True, clear_label=clear_label)
    if sd.kind == "audio_lang":
        cur = overrides.get(sd.key) or ""
        return await _pick_language(
            sd, cur, allow_clear=True, clear_label=_t("settings_clear_autodetect"), audio_pool=True
        )
    if sd.kind == "slot_model":
        slot = _slot_from_key(sd.key)
        return await _pick_provider_and_model(sd, slot, repo=repo, settings=s)
    if sd.kind == "api_key":
        provider = _provider_from_api_key_row(sd.key)
        if provider == "local":
            # `_edit_local_base_url` handles the URL prompt; smoke
            # check the new URL afterwards so the user gets a clear
            # ✓/✗ before leaving the row.
            result = await _edit_local_base_url(repo, s)
            if result == _SENTINEL_KEY_SAVED:
                from unread.ai.model_listing import clear_verified_cache, verify_provider

                clear_verified_cache("local")
                ok, msg = await verify_provider("local", get_settings())
                if ok:
                    console.print(f"[green]{_tf('settings_smoke_ok', provider='local')}[/]")
                else:
                    console.print(f"[yellow]{_tf('settings_smoke_fail', provider='local', err=msg)}[/]")
            return result
        changed = await _manage_provider_key(provider)
        if changed:
            _reload_settings_singleton()
            # After a key edit, smoke-test so the user sees ✓ or ✗
            # immediately. Failures don't block — the edit is already
            # persisted and the user can retry from the same row.
            from unread.ai.model_listing import clear_verified_cache, verify_provider

            clear_verified_cache(provider)
            console.print(f"[grey70]{_tf('settings_smoke_running', provider=provider)}[/]")
            ok, msg = await verify_provider(provider, get_settings())
            if ok:
                console.print(f"[green]{_tf('settings_smoke_ok', provider=provider)}[/]")
            else:
                console.print(f"[yellow]{_tf('settings_smoke_fail', provider=provider, err=msg)}[/]")
            return _SENTINEL_KEY_SAVED
        return None
    if sd.kind == "bool":
        cur_bool = bool(_read_attr(s, sd.key))
        return await _pick_bool(sd, cur_bool)
    if sd.kind == "int":
        cur_int = int(_read_attr(s, sd.key))
        return await _pick_int(sd, cur_int)
    if sd.kind == "log_mode":
        cur_mode = str(s.logging.mode or "normal")
        return await _pick_log_mode(sd, cur_mode)
    if sd.kind == "string":
        cur = str(overrides.get(sd.key) or _read_attr(s, sd.key) or "")
        return await _pick_string(sd, cur)
    return None


def _read_attr(s: Any, key: str) -> Any:
    section, attr = key.split(".", 1)
    return getattr(getattr(s, section), attr)


_SENTINEL_LANG_CUSTOM = "__settings_lang_custom__"


async def _pick_language(
    sd: SettingDef,
    current: str,
    *,
    allow_clear: bool,
    clear_label: str = "",
    audio_pool: bool = False,
    strict_ui: bool = False,
) -> str | None:
    """Language picker.

    `strict_ui=True` locks the picker to the i18n-supported pool (UI
    language) — no Custom-code row, no validation against ISO 639-1.
    For report_language / content_language / audio_language, the pool
    is just a starting shortlist and the user can pick "Custom code..."
    to enter any valid ISO 639-1 code.
    """
    from unread.i18n import LANGUAGE_NAMES
    from unread.util.languages import language_display_name, normalize_language_code
    from unread.util.prompt import Choice, ask_text
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    if strict_ui:
        pool = _supported_ui_languages()
    elif audio_pool:
        pool = _supported_audio_languages()
    else:
        pool = _supported_llm_languages()
    if current and current not in pool:
        pool = [current, *pool]

    items: list = []
    for code in pool:
        # Prefer the i18n display name (translated where available) and
        # fall back to the ISO 639-1 English name for the long tail.
        name = LANGUAGE_NAMES.get(code) or language_display_name(code)
        marker = "  ★" if code == current else ""
        items.append(Choice(value=code, label=f"{code:<4} — {name}{marker}"))
    if not strict_ui:
        items.append(_sep())
        items.append(Choice(value=_SENTINEL_LANG_CUSTOM, label=_t("settings_lang_custom_choice")))
    if allow_clear:
        items.append(_sep())
        items.append(Choice(value=_SENTINEL_CLEAR, label=clear_label))
    items.append(_sep())
    items.append(Choice(value=_SENTINEL_KEEP, label=_t("settings_keep_current")))
    items.append(Choice(value=_SENTINEL_EXIT, label=_t("settings_exit_row")))

    default = current if current in pool else _SENTINEL_KEEP
    try:
        picked = _select(
            sd.label,
            choices=items,
            default_value=default,
            instruction=_compose_instruction(sd.desc),
            erase=True,
        )
    except KeyboardInterrupt:
        # ESC inside a sub-picker is "back to settings menu", not "exit".
        return None
    if picked is None:
        return _SENTINEL_EXIT
    if picked == _SENTINEL_KEEP:
        return None
    if picked == _SENTINEL_CLEAR:
        return _SENTINEL_CLEAR
    if picked == _SENTINEL_EXIT:
        return _SENTINEL_EXIT
    if picked == _SENTINEL_LANG_CUSTOM:
        while True:
            try:
                raw = ask_text(_t("settings_lang_custom_prompt"), default="", erase=True)
            except KeyboardInterrupt:
                return None
            if raw is None:
                return None
            raw = raw.strip()
            if not raw:
                return None
            code = normalize_language_code(raw)
            if code is None:
                console.print(f"[red]{_tf('settings_lang_invalid', raw=raw)}[/]")
                continue
            if audio_pool:
                from unread.util.languages import WHISPER_LANGUAGES

                if code not in WHISPER_LANGUAGES:
                    console.print(f"[yellow]{_tf('settings_lang_not_whisper', code=code)}[/]")
            if code == current:
                return None
            return code
    if picked == current:
        return None
    return picked


async def _pick_provider_and_model(sd: SettingDef, slot: str, *, repo, settings) -> str | None:
    """Two-step compound picker for one slot's `(provider, model)` pair.

    Step 1: pick the provider, filtered by `_SLOT_PROVIDERS[slot]`
    (audio is restricted to providers with a Whisper-shape API).
    Step 2: pick the model from that provider's role-filtered catalog,
    reusing :func:`_pick_model`.

    Both `ai.<slot>_provider` and `ai.<slot>_model` are written to the
    DB — atomically, in one transaction (provider first, model second).
    Returns :data:`_SENTINEL_KEY_SAVED` on any change so the main loop
    bumps `saved_anything` without trying to apply the synthetic key
    via the standard override path. Returns ``None`` when the user
    backed out without changing anything.
    """
    from unread.util.prompt import Choice
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    current_provider = (getattr(settings.ai, f"{slot}_provider", "") or "").strip().lower()
    current_model = (getattr(settings.ai, f"{slot}_model", "") or "").strip()
    options = _SLOT_PROVIDERS.get(slot, ("openai",))

    # Step 1 — provider picker. Pre-select the slot's current provider
    # so the user can re-edit just the model with two Enter presses.
    items: list = []
    for opt in options:
        marker = "  ★" if opt == current_provider else ""
        items.append(Choice(value=opt, label=f"{opt}{marker}"))
    items.append(_sep())
    items.append(Choice(value=_SENTINEL_KEEP, label=_t("settings_keep_current")))
    items.append(Choice(value=_SENTINEL_EXIT, label=_t("settings_exit_row")))

    default = current_provider if current_provider in options else _SENTINEL_KEEP
    cur_render = f"{current_provider} | {current_model}" if current_provider else _t("settings_value_unset")
    try:
        picked_provider = _select(
            sd.label,
            choices=items,
            default_value=default,
            instruction=_compose_instruction(
                sd.desc,
                current=cur_render,
                prefix=_tf("settings_slot_step1_prefix", slot=slot),
            ),
            erase=True,
        )
    except KeyboardInterrupt:
        return None
    if picked_provider is None or picked_provider == _SENTINEL_KEEP:
        return None
    if picked_provider == _SENTINEL_EXIT:
        return _SENTINEL_EXIT

    # Step 1.5 — provider readiness check. For keyed providers, prompt
    # for a missing key inline. For local, smoke-test the URL and offer
    # to fix it if unreachable. After any credential change, re-run the
    # smoke test so the user sees confirmation that things actually
    # work before we let them pick a model that would fail on first use.
    ready = await _verify_or_setup_provider(repo, picked_provider)
    if not ready:
        return None

    # Step 2 — model picker scoped to the picked provider's role catalog.
    role = _SLOT_ROLE.get(slot, "chat")
    # If provider didn't change, preselect the current model so a quick
    # `Enter Enter` no-ops cleanly. If provider changed, current_model
    # almost certainly belongs to the old catalog — don't preselect it.
    model_default = current_model if picked_provider == current_provider else ""
    new_model = await _pick_model_for_slot(sd, picked_provider, role, model_default, settings=settings)
    if new_model is None:
        return None
    if new_model == _SENTINEL_EXIT:
        return _SENTINEL_EXIT

    # Persist both keys. Empty model means "use the provider's class
    # default" — drop the override row instead of writing an empty string.
    provider_key = f"ai.{slot}_provider"
    model_key = f"ai.{slot}_model"
    if picked_provider:
        await repo.set_app_setting(provider_key, picked_provider)
        _apply_one_override(get_settings(), provider_key, picked_provider)
    if new_model:
        await repo.set_app_setting(model_key, new_model)
        _apply_one_override(get_settings(), model_key, new_model)
    elif await repo.delete_app_setting(model_key):
        _apply_one_override(get_settings(), model_key, "")
    return _SENTINEL_KEY_SAVED


def _allow_custom_model(provider: str, role: str) -> bool:
    """Whether to expose the "Custom…" text-input row for this slot.

    Closed catalogs (audio across every provider; anthropic + google
    chat / vision) drop Custom — entering a non-existent model would
    just 4xx at call time. Open catalogs (local servers; openai +
    openrouter chat-class roles) keep it for power users with private
    fine-tunes or dated revisions.
    """
    if provider == "local":
        return True
    if role == "audio":
        return False
    return provider not in {"anthropic", "google"}


async def _pick_model_for_slot(
    sd: SettingDef, provider: str, role: str, current: str, *, settings: Any
) -> str | None:
    """Step 2 of the compound picker: pick a model from `provider`'s catalog.

    Renders the curated catalog (`unread.ai.models.models_for_provider`)
    plus any model IDs the live API previously returned (via
    :mod:`unread.ai.model_listing`). The "(use provider's default: <id>)"
    row shows the resolved default name so the user knows what they're
    choosing instead of an opaque placeholder. The "🔄 Reload from API"
    row drops the per-process cache and re-runs the picker — useful
    when a provider ships a new model.

    Returns the picked model string, the empty string (for "use
    provider default → drop the slot's `_model` override"), `None`
    (back out, no change), or `_SENTINEL_EXIT`.
    """
    from unread.ai.model_listing import cached_models, clear_cache, fetch_models, is_cached
    from unread.ai.models import models_for_provider
    from unread.ai.providers import provider_default_model
    from unread.util.prompt import Choice, ask_text
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    catalog = models_for_provider(provider, role=role)
    catalog_ids = [m.id for m in catalog]
    fetched_ids = cached_models(provider, role)
    pool_ids: list[str] = []
    seen: set[str] = set()
    if current:
        pool_ids.append(current)
        seen.add(current)
    for cid in catalog_ids:
        if cid not in seen:
            pool_ids.append(cid)
            seen.add(cid)
    for fid in fetched_ids:
        if fid not in seen:
            pool_ids.append(fid)
            seen.add(fid)

    if not catalog and not fetched_ids and provider == "local":
        catalog_note = _t("settings_local_no_catalog")
    elif not catalog and not fetched_ids:
        catalog_note = _t("settings_no_pricing_models")
    else:
        catalog_note = _tf("settings_models_for_provider", provider=provider)

    catalog_by_id = {m.id: m for m in catalog}
    fetched_set = set(fetched_ids)
    items: list = []
    for name in pool_ids:
        marker = "  ★" if name == current else ""
        info = catalog_by_id.get(name)
        if info is None:
            tag = "  [API]" if name in fetched_set else ""
            label = f"{name}{tag}{marker}"
        elif role == "audio":
            label = f"{info.label}  [{name}]  ${info.input_price:g}/min{marker}"
        elif info.input_price > 0:
            label = f"{info.label}  [{name}]  ${info.input_price:g}/${info.output_price:g}{marker}"
        else:
            label = f"{info.label}  [{name}]{marker}"
        items.append(Choice(value=name, label=label))
    if pool_ids:
        items.append(_sep())
    default_id = provider_default_model(provider, role)
    if default_id:
        default_label = _tf("settings_slot_use_default_named", value=default_id)
    else:
        default_label = _t("settings_slot_use_default")
    items.append(Choice(value="__use_default__", label=default_label))
    if _allow_custom_model(provider, role):
        items.append(Choice(value="__custom__", label=_t("settings_custom_model_row")))
    reload_label = (
        _t("settings_reload_models_row") if is_cached(provider, role) else _t("settings_fetch_models_row")
    )
    items.append(Choice(value="__reload__", label=reload_label))
    items.append(_sep())
    items.append(Choice(value=_SENTINEL_KEEP, label=_t("settings_keep_current")))
    items.append(Choice(value=_SENTINEL_EXIT, label=_t("settings_exit_row")))

    default = current if current in pool_ids else "__use_default__"
    try:
        picked = _select(
            sd.label,
            choices=items,
            default_value=default,
            instruction=_compose_instruction(sd.desc, current=current, prefix=catalog_note),
            erase=True,
        )
    except KeyboardInterrupt:
        return None
    if picked is None or picked == _SENTINEL_KEEP:
        return None
    if picked == _SENTINEL_EXIT:
        return _SENTINEL_EXIT
    if picked == "__use_default__":
        return ""
    if picked == "__reload__":
        console.print(f"[grey70]{_tf('settings_reload_running', provider=provider)}[/]")
        clear_cache(provider, role)
        try:
            fresh = await fetch_models(provider, role, settings)
        except Exception as e:
            console.print(f"[yellow]{_tf('settings_reload_failed', err=str(e)[:200])}[/]")
            fresh = []
        if fresh:
            console.print(f"[green]{_tf('settings_reload_ok', n=len(fresh))}[/]")
        else:
            console.print(f"[grey70]{_t('settings_reload_empty')}[/]")
        return await _pick_model_for_slot(sd, provider, role, current, settings=settings)
    if picked == "__custom__":
        try:
            raw = ask_text(_tf("settings_custom_model_prompt", key=sd.key), default="", erase=True)
        except KeyboardInterrupt:
            return None
        if not raw:
            return None
        return raw.strip()
    return picked


async def _pick_log_mode(sd: SettingDef, current: str) -> str | None:
    """4-way picker for `logging.mode`: silent / normal / verbose / debug.

    Each row shows `<name> — <hint>` so the user gets both the value
    they'd pass on the CLI (`-q` etc.) and a one-line description of
    what it changes. The current value gets a `★` so the user sees
    the active choice without scrolling away to compare.
    """
    from unread.util.prompt import Choice
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    modes = ("silent", "normal", "verbose", "debug")
    items: list[Any] = []
    for m in modes:
        name = _t(f"set_log_mode_{m}")
        hint = _t(f"set_log_mode_{m}_hint")
        marker = "  ★" if m == current else ""
        items.append(Choice(value=m, label=f"{name} — {hint}{marker}"))
    items.append(_sep())
    items.append(Choice(value=_SENTINEL_KEEP, label=_t("settings_keep_current")))
    items.append(Choice(value=_SENTINEL_EXIT, label=_t("settings_exit_row")))

    try:
        picked = _select(
            sd.label,
            choices=items,
            default_value=current if current in modes else "normal",
            instruction=_compose_instruction(sd.desc),
            erase=True,
        )
    except KeyboardInterrupt:
        return None
    if picked is None or picked == _SENTINEL_KEEP:
        return None
    if picked == _SENTINEL_EXIT:
        return _SENTINEL_EXIT
    if picked == current:
        return None
    return picked


async def _pick_bool(sd: SettingDef, current: bool) -> str | None:
    """Render a 3-option toggle: On / Off / Keep current."""
    from unread.util.prompt import Choice
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    on_label = _t("settings_state_on")
    off_label = _t("settings_state_off")
    items = [
        Choice(value="1", label=f"{on_label}{'  ★' if current else ''}"),
        Choice(value="0", label=f"{off_label}{'  ★' if not current else ''}"),
        _sep(),
        Choice(value=_SENTINEL_KEEP, label=_t("settings_keep_current")),
        Choice(value=_SENTINEL_EXIT, label=_t("settings_exit_row")),
    ]
    try:
        picked = _select(
            sd.label,
            choices=items,
            default_value="1" if current else "0",
            instruction=_compose_instruction(sd.desc),
            erase=True,
        )
    except KeyboardInterrupt:
        return None
    if picked is None or picked == _SENTINEL_KEEP:
        return None
    if picked == _SENTINEL_EXIT:
        return _SENTINEL_EXIT
    new_bool = picked == "1"
    if new_bool == current:
        return None
    return picked


async def _verify_or_setup_provider(repo, provider: str) -> bool:
    """Ensure `provider` has credentials and is reachable.

    For keyed providers (openai / openrouter / anthropic / google):
    if no key is stored, prompt for one inline; either way, run a
    smoke test (`models.list()` — no tokens burned) to confirm the
    key works. For local: smoke-test the configured base URL and
    offer to edit it on failure.

    Returns True iff the provider is ready to use OR the user
    explicitly chose to continue without verifying. Returns False
    if the user backed out (cancels the slot edit upstream).
    """
    from unread.ai.model_listing import clear_verified_cache, verify_provider
    from unread.util.prompt import confirm

    name = provider.strip().lower()
    if name == "local":
        # Smoke first; if reachable, no prompt at all (zero friction).
        ok, msg = await verify_provider("local", get_settings())
        if ok:
            console.print(f"[green]{_tf('settings_smoke_ok', provider=name)}[/]")
            return True
        # Unreachable. Show the failure and offer to edit the URL.
        console.print(
            f"[yellow]{_tf('settings_smoke_local_unreachable', url=get_settings().local.base_url, err=msg)}[/]"
        )
        try:
            edit = confirm(_t("settings_smoke_local_edit_q"), default=True)
        except KeyboardInterrupt:
            return False
        if edit:
            await _edit_local_base_url(repo, get_settings())
            _reload_settings_singleton()
            clear_verified_cache("local")
            ok, msg = await verify_provider("local", get_settings())
            if ok:
                console.print(f"[green]{_tf('settings_smoke_ok', provider=name)}[/]")
                return True
            console.print(f"[yellow]{_tf('settings_smoke_fail', provider=name, err=msg)}[/]")
        # Still failing — let the user decide whether to keep going
        # (maybe the server is offline now but will be up later).
        try:
            return confirm(_t("settings_smoke_continue"), default=False)
        except KeyboardInterrupt:
            return False

    # Keyed providers — set the key first if missing, then smoke-test.
    secret_key = _PROVIDER_SECRET_KEYS.get(name)
    if secret_key is None:
        return True  # unknown provider, no readiness check we can run
    settings = get_settings()
    if not _provider_api_key(settings, name):
        console.print(f"[grey70]{_tf('settings_provider_no_key_prompt', provider=name)}[/]")
        saved = await _prompt_and_save_provider_key(name, secret_key, replacing=False)
        if not saved:
            try:
                return confirm(_t("settings_smoke_continue"), default=False)
            except KeyboardInterrupt:
                return False
        _reload_settings_singleton()
    # Always re-test after a key change; cache hit when key was already set.
    clear_verified_cache(name)
    console.print(f"[grey70]{_tf('settings_smoke_running', provider=name)}[/]")
    ok, msg = await verify_provider(name, get_settings())
    if ok:
        console.print(f"[green]{_tf('settings_smoke_ok', provider=name)}[/]")
        return True
    console.print(f"[yellow]{_tf('settings_smoke_fail', provider=name, err=msg)}[/]")
    try:
        return confirm(_t("settings_smoke_continue"), default=False)
    except KeyboardInterrupt:
        return False


async def _manage_provider_key(provider: str) -> bool:
    """Inline editor for `provider`'s API key. Returns True iff DB changed.

    Behaviour:
      - `local` provider has no API key — silently skipped.
      - When no key is stored: offer "Set now" / "Skip".
      - When a key is stored: offer "Update" / "Delete" / "Keep current",
        showing a masked preview of the current value.

    Writes go through `unread.secrets.write_secrets` /
    `unread.secrets.delete_secret` so they land in whichever backend
    `unread security status` reports as active (db / keychain /
    passphrase). Key reads come from the live settings singleton
    (`secrets.read_secrets` already resolved the backend at load time),
    so the read+write halves stay symmetric.
    """
    name = provider.strip().lower()
    secret_key = _PROVIDER_SECRET_KEYS.get(name)
    if secret_key is None:
        return False

    from unread.util.prompt import Choice
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    s = get_settings()
    existing = _provider_api_key(s, name)

    if existing:
        masked = _mask_secret(existing)
        items: list = [
            Choice(value="update", label=_t("settings_provider_key_update")),
            Choice(value="delete", label=_t("settings_provider_key_delete")),
            _sep(),
            Choice(value="keep", label=_t("settings_keep_current")),
        ]
        try:
            picked = _select(
                _tf("settings_provider_key_prompt", provider=name),
                choices=items,
                default_value="keep",
                instruction=_compose_instruction(
                    _tf("settings_provider_key_set", provider=name, masked=masked)
                ),
                erase=True,
            )
        except KeyboardInterrupt:
            return False
        if picked is None or picked == "keep":
            return False
        if picked == "delete":
            from unread.secrets import delete_secret as _delete_secret_via_backend

            removed = await _delete_secret_via_backend(get_settings(), secret_key)
            if removed:
                console.print(f"[green]{_tf('settings_provider_key_deleted', provider=name)}[/]")
            return bool(removed)
        return await _prompt_and_save_provider_key(name, secret_key, replacing=True)

    # No key stored.
    items = [
        Choice(value="set", label=_t("settings_provider_key_set_now")),
        _sep(),
        Choice(value="skip", label=_t("settings_provider_key_skip")),
    ]
    try:
        picked = _select(
            _tf("settings_provider_key_prompt", provider=name),
            choices=items,
            default_value="set",
            instruction=_compose_instruction(_tf("settings_provider_key_unset", provider=name)),
            erase=True,
        )
    except KeyboardInterrupt:
        return False
    if picked != "set":
        return False
    return await _prompt_and_save_provider_key(name, secret_key, replacing=False)


async def _edit_local_base_url(repo, s: Any) -> str | None:
    """Inline editor for `local.base_url`.

    Replaces the API-key flow when the active provider is `local` —
    self-hosted servers don't use an API key, so the only thing worth
    editing here is the host:port (Ollama / LM Studio / vLLM URL).
    Empty input clears the override (reverts to config default
    `http://localhost:11434/v1`).

    Returns `_SENTINEL_KEY_SAVED` on any change so the main loop bumps
    `saved_anything` without going through the standard app_settings
    write path (we already wrote the override directly).
    """
    from unread.util.prompt import ask_text

    current = str(s.local.base_url or "")
    try:
        raw = ask_text(
            _tf("settings_local_base_url_prompt", current=current or "(unset)"),
            default=current,
            erase=True,
        )
    except KeyboardInterrupt:
        return None
    if raw is None:
        return None
    new = raw.strip()
    if new == current:
        return None
    if not new:
        # Empty = drop the override → fall back to config / default.
        removed = await repo.delete_app_setting("local.base_url")
        if removed:
            _reload_settings_singleton()
            console.print(f"[green]{_t('settings_local_base_url_cleared')}[/]")
            return _SENTINEL_KEY_SAVED
        return None
    await repo.set_app_setting("local.base_url", new)
    _apply_one_override(get_settings(), "local.base_url", new)
    console.print(f"[green]{_tf('settings_local_base_url_saved', url=new)}[/]")
    return _SENTINEL_KEY_SAVED


async def _prompt_and_save_provider_key(provider: str, secret_key: str, *, replacing: bool) -> bool:
    """Prompt for a key with hidden input, persist via the active backend."""
    from unread.secrets import write_secrets
    from unread.util.prompt import ask_text

    # Fold the URL hint into the prompt's own message so it's part of
    # the rendered region — `erase=True` then wipes it on submit along
    # with the question, instead of the URL lingering in scrollback once
    # the user has pasted (or skipped) the key.
    info_url = _PROVIDER_KEY_URLS.get(provider, "")
    base_msg = _tf("settings_provider_key_input", provider=provider)
    prompt_msg = f"{base_msg}\n  ({provider} keys: {info_url})" if info_url else base_msg
    try:
        raw_in = ask_text(prompt_msg, default="", password=True, erase=True)
    except KeyboardInterrupt:
        return False
    raw = (raw_in or "").strip()
    if not raw:
        console.print(f"[grey70]{_t('settings_provider_key_empty')}[/]")
        return False
    await write_secrets(get_settings(), {secret_key: raw})
    label = "settings_provider_key_updated" if replacing else "settings_provider_key_saved"
    console.print(f"[green]{_tf(label, provider=provider)}[/]")
    return True


def _provider_api_key(s: Any, provider: str) -> str:
    """Return the resolved API key for `provider` from the live settings.

    Reading from the singleton means this honours whatever backend
    `read_secrets` selected (DB / keychain / passphrase) — we don't care
    which, only whether a key is currently usable.
    """
    name = provider.strip().lower()
    if name == "openai":
        return s.openai.api_key or ""
    if name == "openrouter":
        return s.openrouter.api_key or ""
    if name == "anthropic":
        return s.anthropic.api_key or ""
    if name == "google":
        return s.google.api_key or ""
    return ""


def _mask_secret(value: str) -> str:
    """Show first 4 and last 4 of a key, redacting the middle."""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


async def _pick_string(sd: SettingDef, current: str) -> str | None:
    """Free-text string input. Empty input clears the override."""
    from unread.util.prompt import ask_text

    try:
        raw = ask_text(
            f"{sd.label} — current: {current or '(unset)'}\n{sd.desc}\n",
            default=current,
            erase=True,
        )
    except KeyboardInterrupt:
        return None
    if raw is None:
        return _SENTINEL_EXIT
    raw = raw.strip()
    if raw.lower() in {"q", "quit", "exit"}:
        return _SENTINEL_EXIT
    if not raw:
        return _SENTINEL_CLEAR
    if raw == current:
        return None
    return raw


async def _pick_int(sd: SettingDef, current: int) -> str | None:
    """Free-text integer input with validation. Re-prompts on garbage."""
    from unread.util.prompt import ask_text

    while True:
        try:
            raw = ask_text(
                _tf("settings_int_prompt", label=sd.label, desc=sd.desc, current=current),
                default="",
                erase=True,
            )
        except KeyboardInterrupt:
            return None
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
    """Modal confirm before wiping every override.

    ESC here means "back to the menu" — match the rest of the settings
    pickers, where ESC is non-destructive. Returning False keeps every
    override; the menu loop just re-renders.
    """
    from unread.util.prompt import confirm as _confirm

    try:
        return _confirm(_tf("settings_drop_n_q", n=n), default=False, erase=True)
    except KeyboardInterrupt:
        return False


# ----------------------------- Language pools ------------------------------


def _supported_ui_languages() -> list[str]:
    """ISO codes the UI itself can render in: needs both i18n entries
    AND preset tree (the wizard / report headings reuse the preset tree).

    Today this resolves to en/ru only — and that's correct: the UI
    language axis is bounded by what's translated. Expanding it requires
    contributing both i18n entries and a preset directory.
    """
    from unread.analyzer.prompts import PRESETS_DIR
    from unread.i18n import _STRINGS

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


# Back-compat alias — older call sites import the old name.
_supported_locale_languages = _supported_ui_languages


def _supported_llm_languages() -> list[str]:
    """Pool for `report_language` and `content_language` (source hint).

    LLM-bounded, not i18n-bounded — the analyzer can write a report in
    any language the model knows. Returns the ordered popular shortlist
    from :mod:`unread.util.languages`. The picker also offers a "Custom
    code..." escape hatch for anything outside this list, validated
    against the full ISO 639-1 catalog.
    """
    from unread.util.languages import POPULAR_CODES

    return list(POPULAR_CODES)


def _supported_audio_languages() -> list[str]:
    """Whisper hint pool: popular shortlist filtered to Whisper-supported codes."""
    from unread.util.languages import POPULAR_CODES, WHISPER_LANGUAGES

    return [c for c in POPULAR_CODES if c in WHISPER_LANGUAGES]
