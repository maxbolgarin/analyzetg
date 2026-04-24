"""Configuration loading: .env + config.toml → typed settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    session_path: Path = Path("storage/session.sqlite")
    max_msgs_per_minute: int = 3000


class OpenAICfg(_StrictCfg):
    api_key: str = ""
    chat_model_default: str = "gpt-5.4"
    filter_model_default: str = "gpt-5.4-nano"
    audio_model_default: str = "gpt-4o-mini-transcribe"
    audio_language: str = "ru"
    request_timeout_sec: int = 120
    max_retries: int = 5
    temperature: float = 0.2


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
    tmp_dir: Path = Path("storage/media")
    ffmpeg_path: str = "ffmpeg"


class AnalyzeCfg(_StrictCfg):
    min_msg_chars: int = 3
    output_budget_tokens: int = 1500
    safety_margin_tokens: int = 4000
    chunk_soft_break_minutes: int = 30
    dedupe_forwards: bool = True
    map_concurrency: int = 4


class EnrichCfg(_StrictCfg):
    """Per-media-type enrichment toggles and model choices.

    Defaults preserve today's behavior (voice/videonote transcription ON) while
    keeping the newer enrichers (image/doc/video/link) opt-in so a plain
    `atg analyze` never quietly racks up vision-API spend. Override per-run
    via CLI flags, per-preset via frontmatter, or here for persistent defaults.
    """

    voice: bool = True
    videonote: bool = True
    video: bool = False
    image: bool = False
    doc: bool = False
    link: bool = True
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


class RetentionCfg(_StrictCfg):
    message_retention_days: int = 0
    keep_transcripts_forever: bool = True
    keep_analysis_cache_forever: bool = True


class StorageCfg(_StrictCfg):
    data_path: Path = Path("storage/data.sqlite")


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
    sync: SyncCfg = Field(default_factory=SyncCfg)
    media: MediaCfg = Field(default_factory=MediaCfg)
    analyze: AnalyzeCfg = Field(default_factory=AnalyzeCfg)
    enrich: EnrichCfg = Field(default_factory=EnrichCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    pricing: PricingCfg = Field(default_factory=PricingCfg)

    config_path: Path = Path("config.toml")


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
    """Load settings from .env + config.toml + environment.

    Precedence (high → low):
      1. Shell env vars already exported
      2. .env file (working dir)
      3. config.toml values
      4. dataclass defaults
    """
    _load_dotenv(Path(".env"))

    cfg_path = Path(config_path or os.environ.get("ANALYZETG_CONFIG_PATH", "config.toml"))
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
