"""Test-suite fixtures.

Three concerns:

1. **Per-process `UNREAD_HOME` isolation.** The CLI bootstrap at
   `unread.cli` import time calls `apply_db_overrides_sync(get_settings())`,
   which resolves paths under `unread_home()`. If we don't pin
   `UNREAD_HOME` to a tmp dir BEFORE any `unread.*` import, the test
   suite reads the developer's real `~/.unread/storage/data.sqlite` —
   leaking saved settings across runs and (worse) writing to the
   developer's profile from inside a test. We set the env var at
   module-load time, before any other import in any test module
   collects.

2. **Tmpdir cleanup.** Pre-prod review: the previous `mkdtemp` had no
   teardown — every test session leaked an `unread-tests-*` dir under
   the system tmp. Cumulative bloat for developers running the suite
   thousands of times. `atexit.register` cleans up on interpreter exit;
   `ignore_errors=True` keeps a half-cleaned tree from raising on
   teardown (some tests intentionally chmod files to test guards).

3. **Locale-override leak guard.** Once `UNREAD_HOME` is pinned, the
   bootstrap should be a no-op (the tmp DB doesn't exist on first run).
   The autouse fixture below resets the singleton around every test as
   defense-in-depth — tests that explicitly mutate the settings
   singleton mustn't bleed into neighbours.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# CRITICAL: this assignment runs at module-load time, before pytest
# collects test modules and before any `from unread.*` import resolves.
# Fixture-scoped or session-scoped fixtures are too late — the bootstrap
# at `unread.cli:25` runs at import.
_TESTS_HOME = tempfile.mkdtemp(prefix="unread-tests-")
os.environ.setdefault("UNREAD_HOME", _TESTS_HOME)


@atexit.register
def _cleanup_tests_home() -> None:
    """Remove the per-session tmpdir on interpreter exit.

    Idempotent — pytest may run multiple sessions in one process under
    `pytest-xdist`, and the dir may already be gone if a test cleaned
    up. `ignore_errors=True` because a few tests intentionally chmod
    files to test the .env permission guards; those leave un-removable
    nodes that we don't want raising on teardown.
    """
    # Only clean up if WE created it (not if the user pre-set UNREAD_HOME).
    if os.environ.get("UNREAD_HOME") == _TESTS_HOME:
        shutil.rmtree(_TESTS_HOME, ignore_errors=True)


# Fake credentials so per-command gates (e.g. `cmd_ask`'s OpenAI check,
# `build_client`'s Telegram check) don't bail in unrelated tests. Tests
# that specifically exercise the missing-credential path can call
# `monkeypatch.delenv("OPENAI_API_KEY", raising=False)` to clear them.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake")
os.environ.setdefault("TELEGRAM_API_ID", "111111")
os.environ.setdefault("TELEGRAM_API_HASH", "fakehashfortests")

# Drop the production Scrypt cost down to a test-friendly value. The
# real KDF (n=2**18) runs at ~200 ms per derive — multiplied across the
# crypto / passphrase-backend / aead-envelope test files this turns
# into ~30 s of pure key derivation. The override still exercises real
# Scrypt (i.e. the cryptography path), just with cheaper parameters.
# `unread/security/crypto.py` reads this at import time.
os.environ.setdefault("UNREAD_SCRYPT_N", str(2**10))

# E402 (imports not at top): the `UNREAD_HOME` setdefault above MUST run
# before `unread.config` is imported, otherwise the singleton resolves
# to the developer's real ~/.unread/. Don't reorder.
import pytest

from unread.config import reset_settings


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
