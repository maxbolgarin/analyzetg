"""Configuration loading: .env + config.toml → typed settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelegramCfg(BaseModel):
    api_id: int = 0
    api_hash: str = ""
    session_path: Path = Path("storage/session.sqlite")
    max_msgs_per_minute: int = 3000


class OpenAICfg(BaseModel):
    api_key: str = ""
    chat_model_default: str = "gpt-5.4"
    filter_model_default: str = "gpt-5.4-nano"
    audio_model_default: str = "gpt-4o-mini-transcribe"
    audio_language: str = "ru"
    request_timeout_sec: int = 120
    max_retries: int = 5
    temperature: float = 0.2


class SyncCfg(BaseModel):
    default_lookback_days: int = 7
    batch_size: int = 500
    concurrency: int = 3


class MediaCfg(BaseModel):
    transcribe_voice: bool = True
    transcribe_videonote: bool = True
    transcribe_video: bool = False
    max_media_duration_sec: int = 600
    min_media_duration_sec: int = 1
    download_concurrency: int = 3
    tmp_dir: Path = Path("storage/media")
    ffmpeg_path: str = "ffmpeg"


class AnalyzeCfg(BaseModel):
    min_msg_chars: int = 3
    output_budget_tokens: int = 1500
    safety_margin_tokens: int = 2000
    chunk_soft_break_minutes: int = 30
    dedupe_forwards: bool = True
    map_concurrency: int = 4


class RetentionCfg(BaseModel):
    message_retention_days: int = 0
    keep_transcripts_forever: bool = True
    keep_analysis_cache_forever: bool = True


class StorageCfg(BaseModel):
    data_path: Path = Path("storage/data.sqlite")


class ChatPricing(BaseModel):
    input: float
    cached_input: float
    output: float


class PricingCfg(BaseModel):
    chat: dict[str, ChatPricing] = Field(default_factory=dict)
    audio: dict[str, float] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    openai: OpenAICfg = Field(default_factory=OpenAICfg)
    sync: SyncCfg = Field(default_factory=SyncCfg)
    media: MediaCfg = Field(default_factory=MediaCfg)
    analyze: AnalyzeCfg = Field(default_factory=AnalyzeCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    pricing: PricingCfg = Field(default_factory=PricingCfg)

    config_path: Path = Path("config.toml")


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE per line, # comments, optional quotes).

    Populates os.environ for any keys not already set, so existing shell
    exports still win. Silently no-ops if the file doesn't exist.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
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
