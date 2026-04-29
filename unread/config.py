"""Configuration loading: .env + config.toml → typed settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from unread.core.paths import (
    default_config_path,
    default_data_path,
    default_env_path,
    default_media_dir,
    default_session_path,
)


class _StrictCfg(BaseModel):
    """Base for every nested config block.

    `extra="forbid"` surfaces typos — `chat_modle_default = "..."` used to
    be silently dropped. Reason for inheritance over per-class repetition:
    one place to flip the knob if we ever need `extra="allow"` again.
    """

    model_config = ConfigDict(extra="forbid")


class TelegramCfg(_StrictCfg):
    api_id: int = 0
    api_hash: str = ""
    # Resolved lazily via the factory so `UNREAD_HOME` overrides — both
    # in tests and at runtime — flow through without rewriting config.
    session_path: Path = Field(default_factory=default_session_path)
    max_msgs_per_minute: int = 3000


class OpenAICfg(_StrictCfg):
    api_key: str = ""
    chat_model_default: str = "gpt-5.4-mini"
    filter_model_default: str = "gpt-5.4-nano"
    audio_model_default: str = "gpt-4o-mini-transcribe"
    # None / empty → Whisper autodetects per file. Set to an ISO code
    # ("ru", "en", "de", …) when every audio file is the same language —
    # gives slightly faster + more accurate transcription. Decoupled from
    # `locale.language` (UI) so an English UI can still transcribe RU audio.
    audio_language: str | None = None
    request_timeout_sec: int = 120
    max_retries: int = 5
    temperature: float = 0.2


class AICfg(_StrictCfg):
    """Primary chat-completion provider routing.

    `provider` is the single switch that selects which adapter
    `chat_complete()` dispatches to. The OpenAI key still lives in
    `[openai]` (back-compat) and continues to back capabilities the
    other providers can't supply (Whisper transcription, embeddings,
    vision). Per-provider keys for the four alternative providers
    live in their own blocks below.

    `base_url` and `chat_model` / `filter_model` are optional
    overrides — when empty, each provider supplies its own default.
    """

    provider: str = "openai"  # openai | openrouter | anthropic | google | local
    base_url: str = ""  # OpenAI-compatible endpoint override; auto-derived for openrouter
    chat_model: str = ""  # empty → provider's hard-coded default
    filter_model: str = ""  # ditto
    # Safety: when `base_url` resolves to anything outside the per-provider
    # trusted-host allowlist (api.openai.com, api.anthropic.com,
    # generativelanguage.googleapis.com, openrouter.ai, plus localhost/RFC1918
    # for self-hosted), refuse to send the upstream API key. A typo like
    # `api.openai.com.attacker.tld` would otherwise silently exfiltrate the
    # key. Set this to True to acknowledge that you really do mean to send
    # your key to a custom host (corporate proxy, internal gateway, etc.).
    base_url_trusted: bool = False


class OpenRouterCfg(_StrictCfg):
    """OpenRouter routes the OpenAI Chat Completions API to many backends.

    Stored separately so a user can have OpenAI configured for fallback
    capabilities (Whisper / embeddings / vision) while running the
    primary chat through OpenRouter.
    """

    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"


class AnthropicCfg(_StrictCfg):
    api_key: str = ""


class GoogleCfg(_StrictCfg):
    """Google Gen AI (Gemini) — Developer API only for now; Vertex would
    require additional `project` / `location` / ADC plumbing not worth
    the surface increase for v1."""

    api_key: str = ""


class LocalCfg(_StrictCfg):
    """Self-hosted OpenAI-compatible server (Ollama, LM Studio, vLLM, …).

    `base_url` is required. `api_key` defaults to a placeholder so the
    OpenAI SDK doesn't refuse the request — most local servers ignore
    the header but the SDK's client constructor enforces a non-empty
    string.
    """

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "local-no-key"


class SyncCfg(_StrictCfg):
    default_lookback_days: int = 7
    batch_size: int = 500
    concurrency: int = 3


class MediaCfg(_StrictCfg):
    transcribe_voice: bool = True
    transcribe_videonote: bool = True
    transcribe_video: bool = False
    max_media_duration_sec: int = 600
    min_media_duration_sec: int = 1
    download_concurrency: int = 3
    tmp_dir: Path = Field(default_factory=default_media_dir)
    ffmpeg_path: str = "ffmpeg"


class AnalyzeCfg(_StrictCfg):
    min_msg_chars: int = 3
    output_budget_tokens: int = 1500
    safety_margin_tokens: int = 4000
    chunk_soft_break_minutes: int = 30
    dedupe_forwards: bool = True
    map_concurrency: int = 4
    # Threshold for the formatter's `[high-impact]` marker: a message with
    # at least this many reactions (sum across all kinds) gets the marker
    # so the LLM can lean on it for "what mattered" presets. 0 disables.
    high_impact_reactions: int = 3
    # Console rendering: when True, transform `[#N](https://t.me/...)`
    # citations into `#N (https://t.me/...)` so the URL is visible and
    # copy-pasteable. The saved markdown file is unaffected — keep it
    # OSC 8-friendly for terminals that support it. Flip on if you're on
    # macOS Terminal.app or any other terminal without OSC 8 hyperlinks.
    plain_citations: bool = False


class AskCfg(_StrictCfg):
    """Knobs for `unread ask` retrieval and rerank.

    Defaults aim at the typical per-question budget (~$0.01 on
    gpt-5.4-mini): retrieve 500 keyword hits, rerank with the cheap model
    down to 50, send those to the answer model.
    """

    rerank_enabled: bool = True
    rerank_top_k: int = 500  # candidate pool size before rerank
    rerank_keep: int = 50  # what survives rerank → flagship
    rerank_batch_size: int = 50  # messages per cheap-model call
    rerank_model: str | None = None  # None → falls back to filter_model_default


class EnrichCfg(_StrictCfg):
    """Per-media-type enrichment toggles and model choices.

    Defaults preserve today's behavior (voice/videonote transcription ON) while
    keeping the newer enrichers (image/doc/video/link) opt-in so a plain
    `unread analyze` never quietly racks up vision-API spend. Override per-run
    via CLI flags, per-preset via frontmatter, or here for persistent defaults.
    """

    voice: bool = True
    videonote: bool = True
    video: bool = False
    image: bool = False
    doc: bool = False
    # Off by default — link summaries can fire one OpenAI call per unique URL,
    # which surprises users on link-heavy chats. Opt in via --enrich=link, the
    # `links` preset, or `link = true` in config.toml.
    link: bool = False
    vision_model: str = "gpt-4o-mini"
    doc_model: str | None = None  # None → falls back to filter_model
    link_model: str | None = None  # None → falls back to filter_model
    max_images_per_run: int = 50
    max_link_fetches_per_run: int = 50
    # 25 MB ceiling on document downloads. Matches the OpenAI audio cap we
    # already use for voice/video, and covers the vast majority of real
    # PDFs/DOCX files (a 50-page technical PDF typically runs 3-8 MB).
    # The *text extract* from any doc is separately capped to `max_doc_chars`
    # so a huge PDF can't flood the analysis prompt even if we download it.
    max_doc_bytes: int = 25_000_000
    max_doc_chars: int = 20_000
    link_fetch_timeout_sec: int = 10
    skip_link_domains: list[str] = Field(default_factory=list)
    concurrency: int = 3


class WebsiteCfg(_StrictCfg):
    """Knobs for `unread analyze <website-url>` page fetch + extraction.

    Tuned higher than the per-message link enricher: a website analysis
    expects to consume the full article body (50k+ chars), not a 1-2 sentence
    summary. `max_html_bytes` is the post-fetch cap; oversize pages are
    silently truncated rather than rejected so a single huge page doesn't
    cancel the run.
    """

    fetch_timeout_sec: int = 30
    max_html_bytes: int = 5_000_000  # 5 MB hard cap on raw HTML
    max_paragraphs: int = 400  # post-split cap on synthetic messages
    # Browser-shaped UA: many CDNs (Cloudflare, Fastly) and CMSes return a
    # minimal interstitial when the UA looks bot-like. The bot-shaped string
    # used by the link enricher is fine for one-shot summaries but trips
    # full-article fetches more often than not.
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    )


class RetentionCfg(_StrictCfg):
    message_retention_days: int = 0
    keep_transcripts_forever: bool = True
    keep_analysis_cache_forever: bool = True


class StorageCfg(_StrictCfg):
    data_path: Path = Field(default_factory=default_data_path)


class LocaleCfg(_StrictCfg):
    """Output / UI / preset language.

    `language` controls everything user-visible: wizard, formatter labels
    in saved reports, citation/sources heading, ask labels, image+link
    enricher prompts, and which preset directory the loader reads
    (`presets/<language>/...`). The LLM produces analysis output in this
    language because the loaded presets are natively in it.

    `content_language` is the *chat content* language hint — only affects
    cost estimation (`AVG_TOKENS_PER_MSG`) and an optional one-line model
    hint about the chat language. Empty string means "follow `language`".

    Both default to "en" so a fresh install has an English experience;
    Russian users opt in via `language = "ru"` (or `--language ru`).
    """

    language: str = "en"
    content_language: str = ""


class ChatPricing(_StrictCfg):
    input: float
    cached_input: float
    output: float


class PricingCfg(_StrictCfg):
    chat: dict[str, ChatPricing] = Field(default_factory=dict)
    audio: dict[str, float] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    openai: OpenAICfg = Field(default_factory=OpenAICfg)
    ai: AICfg = Field(default_factory=AICfg)
    openrouter: OpenRouterCfg = Field(default_factory=OpenRouterCfg)
    anthropic: AnthropicCfg = Field(default_factory=AnthropicCfg)
    google: GoogleCfg = Field(default_factory=GoogleCfg)
    local: LocalCfg = Field(default_factory=LocalCfg)
    sync: SyncCfg = Field(default_factory=SyncCfg)
    media: MediaCfg = Field(default_factory=MediaCfg)
    analyze: AnalyzeCfg = Field(default_factory=AnalyzeCfg)
    ask: AskCfg = Field(default_factory=AskCfg)
    enrich: EnrichCfg = Field(default_factory=EnrichCfg)
    website: WebsiteCfg = Field(default_factory=WebsiteCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    locale: LocaleCfg = Field(default_factory=LocaleCfg)
    pricing: PricingCfg = Field(default_factory=PricingCfg)

    # Resolved at load time by `load_settings()`. Field default is the
    # cwd-relative fallback that almost never wins — `default_config_path()`
    # under `~/.unread/` and `UNREAD_CONFIG_PATH` both take precedence.
    config_path: Path = Field(default_factory=default_config_path)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        # Surface the path + underlying position so the user can find the
        # typo in seconds instead of guessing from a bare stack trace.
        raise ValueError(
            f"{path}: TOML parse error — {e}. Check for unclosed quotes/brackets and missing commas."
        ) from e


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE per line, # comments, optional quotes).

    Populates os.environ for any keys not already set, so existing shell
    exports still win. Silently no-ops if the file doesn't exist.
    """
    if not path.exists():
        return
    # utf-8-sig transparently strips a UTF-8 BOM if present (common on
    # Windows editors) — without this, the first line parses as
    # "\ufeffTELEGRAM_API_ID" and Telegram login fails with "no API id"
    # with no hint as to why.
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Load settings from .env + config.toml + environment + session DB.

    Precedence (high → low):
      1. Shell env vars already exported
      2. ~/.unread/.env (or `UNREAD_HOME/.env`)
      3. ~/.unread/config.toml (or `UNREAD_CONFIG_PATH`)
      4. Persisted secrets in the Telethon session DB (api_id /
         api_hash / openai_api_key) — only fills fields the higher
         layers left empty, so a populated `.env` always wins.
      5. dataclass defaults

    `.env` and `config.toml` live exclusively under `unread_home()` —
    cwd-relative discovery has been removed so a stray `./config.toml`
    in a checkout can't silently shadow the user's real settings.
    Use `UNREAD_HOME=$(pwd)` to point both at a project directory
    explicitly during development.

    Layer 4 lets a user delete `~/.unread/.env` after the first
    successful `unread tg init` and keep using the CLI — credentials
    are written into the session DB at init time and read back here.
    """
    _load_dotenv(default_env_path())

    # `UNREAD_CONFIG_PATH` is the canonical override.
    cfg_path = Path(config_path or os.environ.get("UNREAD_CONFIG_PATH") or default_config_path())
    raw = _read_toml(cfg_path)

    # Env overrides for secrets
    if "telegram" not in raw:
        raw["telegram"] = {}
    if api_id := os.environ.get("TELEGRAM_API_ID"):
        try:
            raw["telegram"]["api_id"] = int(api_id)
        except ValueError as e:
            raise ValueError(f"TELEGRAM_API_ID must be an integer, got: {api_id!r}") from e
    if api_hash := os.environ.get("TELEGRAM_API_HASH"):
        raw["telegram"]["api_hash"] = api_hash

    if "openai" not in raw:
        raw["openai"] = {}
    if api_key := os.environ.get("OPENAI_API_KEY"):
        raw["openai"]["api_key"] = api_key

    # Back-compat: mirror legacy [media].transcribe_* into [enrich] when the
    # user hasn't declared [enrich] yet. Keeps existing configs working without
    # a forced rewrite.
    media_block = raw.get("media") or {}
    enrich_block = raw.setdefault("enrich", {})
    for legacy_key, new_key in (
        ("transcribe_voice", "voice"),
        ("transcribe_videonote", "videonote"),
        ("transcribe_video", "video"),
    ):
        if legacy_key in media_block and new_key not in enrich_block:
            enrich_block[new_key] = bool(media_block[legacy_key])

    settings = Settings(**raw)
    settings.config_path = cfg_path

    # Layer 4: fill in missing credentials from the session DB. Only
    # touches fields the prior layers left empty, so env / .env always
    # wins on rotation. Imported lazily to avoid a circular import
    # (secrets reads from the session-path field defined here).
    # Always consult the secrets DB — the user may have any subset of
    # provider keys persisted (one per active install), and we want
    # each to overlay onto the matching empty config slot independently.
    import contextlib as _contextlib

    from unread.secrets import read_secrets

    persisted = read_secrets(settings)
    if persisted:
        if not settings.telegram.api_id and (raw_id := persisted.get("telegram.api_id")):
            # Stale row from a corrupt write — ignore, keep going.
            with _contextlib.suppress(ValueError):
                settings.telegram.api_id = int(raw_id)
        if not settings.telegram.api_hash and (h := persisted.get("telegram.api_hash")):
            settings.telegram.api_hash = h
        if not settings.openai.api_key and (k := persisted.get("openai.api_key")):
            settings.openai.api_key = k
        if not settings.openrouter.api_key and (k := persisted.get("openrouter.api_key")):
            settings.openrouter.api_key = k
        if not settings.anthropic.api_key and (k := persisted.get("anthropic.api_key")):
            settings.anthropic.api_key = k
        if not settings.google.api_key and (k := persisted.get("google.api_key")):
            settings.google.api_key = k

    return settings


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy-loaded process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """For tests — force next get_settings() to reload."""
    global _settings
    _settings = None
