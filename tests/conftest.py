"""Test-suite fixtures.

Single concern today: keep `atg.cli`'s startup overlay
(`apply_db_overrides_sync`) from polluting tests with whatever's saved
in the developer's local `storage/data.sqlite`. Without this, a
contributor who runs `atg settings set locale.language ru` in their
working tree sees opaque test failures the next time they run pytest
because `BASE_SYSTEM` (and friends) lazy-resolve through the live
settings singleton at test-collection time.
"""

from __future__ import annotations

import pytest

from atg.config import reset_settings


@pytest.fixture(autouse=True)
def _reset_locale_overrides_before_each_test():
    """Re-read settings from .env + config.toml only — drop any DB-saved
    overrides that may have leaked in via `cli.py`'s import-time
    bootstrap. Runs around every test, so per-test mutations to the
    settings singleton (in tests that explicitly do `s.locale.language = ...`)
    don't bleed into neighbours either.
    """
    reset_settings()
    yield
    reset_settings()
