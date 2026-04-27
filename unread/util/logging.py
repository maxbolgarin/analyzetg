"""Logging: structlog with a Rich console handler."""

from __future__ import annotations

import logging
import os
from typing import Any

import structlog
from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(verbose: bool = False) -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    level = (
        logging.DEBUG
        if verbose or os.environ.get("UNREAD_DEBUG") or os.environ.get("ANALYZETG_DEBUG")
        else logging.INFO
    )

    handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=False,
        rich_tracebacks=True,
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
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name) if name else structlog.get_logger()
