"""Logging: structlog with a Rich console handler."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import structlog
from rich.console import Console
from rich.logging import RichHandler

console = Console()


# Regex shapes for common secret-bearing strings. Any value in a
# log event matching one of these gets masked before rendering.
# Tuned for false-negatives on harmless strings, false-positives are
# fine since redaction is one-way and visible in the output.
_SECRET_VALUE_RE = re.compile(
    r"""
    (?:
        sk-(?:ant-|or-|proj-)?[A-Za-z0-9_\-]{16,}  # OpenAI / Anthropic / OpenRouter
        | AIza[A-Za-z0-9_\-]{30,}                  # Google API
        | gsk_[A-Za-z0-9]{20,}                     # Groq
        | sk_(?:live|test)_[A-Za-z0-9]{20,}        # generic Stripe-style
    )
    """,
    re.VERBOSE,
)

# Event-dict keys that ALWAYS get masked, regardless of value shape.
# Catches Telethon session strings, api_hash values, etc. that don't
# match a regex but live behind an obvious key name.
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_hash",
        "apikey",
        "secret",
        "token",
        "password",
        "passphrase",
        "session_string",
        "auth_key",
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "openrouter_api_key",
    }
)

_REDACTED = "***REDACTED***"


def _redact_processor(_logger: Any, _method_name: str, event_dict: dict) -> dict:
    """structlog processor: mask secret-shaped values and known-secret keys.

    Last line of defense — modules should still avoid logging raw
    credentials. Walks one level into nested dicts/lists so common
    `extra={"payload": {...}}` patterns and Telethon's nested error
    structures don't bypass the filter. Stops at depth 2 to keep the
    cost bounded on every log call.
    """

    def _scrub(key: str | None, value: Any, depth: int) -> Any:
        # Match by key first — covers Telethon session strings, api_hash,
        # etc. that don't match the regex but live behind an obvious key.
        if isinstance(key, str) and key.lower() in _SECRET_KEYS:
            return _REDACTED if value else value
        if isinstance(value, str) and value:
            masked = _SECRET_VALUE_RE.sub(_REDACTED, value)
            return masked
        if depth <= 0:
            return value
        if isinstance(value, dict):
            return {k: _scrub(k if isinstance(k, str) else None, v, depth - 1) for k, v in value.items()}
        if isinstance(value, list | tuple):
            cls = type(value)
            return cls(_scrub(None, item, depth - 1) for item in value)
        return value

    for key, value in list(event_dict.items()):
        event_dict[key] = _scrub(key, value, depth=2)
    return event_dict


def setup_logging(verbose: bool = False) -> None:
    """Configure structlog + stdlib logging. Idempotent.

    Also exports ``UNREAD_VERBOSE=1`` into the environment when called
    with ``verbose=True`` so other modules (notably ``cli._run``'s
    top-level error handler) can decide whether to render a Rich
    traceback or a friendly one-liner without re-plumbing the flag
    through every command body.
    """
    if verbose:
        os.environ["UNREAD_VERBOSE"] = "1"
    level = logging.DEBUG if verbose or os.environ.get("UNREAD_DEBUG") else logging.INFO

    # Rich tracebacks render local variables — including any passphrase
    # / API key still on the stack — to the terminal on any unhandled
    # exception. The structlog redactor only walks top-level event-dict
    # keys, so a Rich traceback bypasses it. Gate the feature behind
    # `verbose=True` (or `UNREAD_VERBOSE=1`) so production runs default
    # to the safe boring traceback that doesn't print locals.
    rich_tracebacks_enabled = bool(verbose or os.environ.get("UNREAD_VERBOSE"))
    handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=False,
        rich_tracebacks=rich_tracebacks_enabled,
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Mute chatty libraries
    for noisy in ("telethon", "httpx", "openai", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_processor,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name) if name else structlog.get_logger()
