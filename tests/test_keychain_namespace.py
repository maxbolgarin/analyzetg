"""Per-install namespacing of the keychain service name.

Pre-prod review found that two `unread` installs on the same OS user
shared a flat `keyring` service name (`"unread"`) and silently clobbered
each other's credentials — and any other Python process could fetch
them by guessing the bare name.

Verifies:

- ``keychain_service()`` differs across two install paths.
- ``keychain_read`` / ``keychain_write`` / ``keychain_delete`` always
  pass the namespaced name to ``keyring``.
- Migration shim: legacy entries written under the bare ``"unread"``
  service are forward-ported to the namespaced name on first read AND
  the legacy entry is deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------- fake keyring backend (mirrors test_secrets_backend.py) -------


def _build_fake_keyring():
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError

    class _FakeKeyring(KeyringBackend):
        priority = 1.0

        def __init__(self) -> None:
            super().__init__()
            self.store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.store[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            if (service, username) not in self.store:
                raise PasswordDeleteError(f"no entry for {service}/{username}")
            del self.store[(service, username)]

    return _FakeKeyring()


@pytest.fixture
def fake_keyring(monkeypatch):
    import keyring

    fake = _build_fake_keyring()
    original = keyring.get_keyring()
    keyring.set_keyring(fake)
    try:
        yield fake
    finally:
        keyring.set_keyring(original)


@pytest.fixture(autouse=True)
def _reset_service_cache():
    """Clear the per-install cache around every test so flips of
    `UNREAD_HOME` from monkeypatched env vars take effect immediately."""
    from unread.secrets_backend import _reset_keychain_service_cache

    _reset_keychain_service_cache()
    yield
    _reset_keychain_service_cache()


# ---------- service-name derivation --------------------------------------


def test_keychain_service_differs_per_install(monkeypatch, tmp_path: Path) -> None:
    """Two distinct install paths yield two distinct service names."""
    install_a = tmp_path / "install_a"
    install_b = tmp_path / "install_b"
    install_a.mkdir()
    install_b.mkdir()

    from unread.secrets_backend import _reset_keychain_service_cache, keychain_service

    monkeypatch.setenv("UNREAD_HOME", str(install_a))
    _reset_keychain_service_cache()
    name_a = keychain_service()

    monkeypatch.setenv("UNREAD_HOME", str(install_b))
    _reset_keychain_service_cache()
    name_b = keychain_service()

    assert name_a != name_b
    assert name_a.startswith("unread:")
    assert name_b.startswith("unread:")


def test_keychain_service_is_stable_for_same_install(monkeypatch, tmp_path: Path) -> None:
    """Same install path → same service name across calls."""
    install = tmp_path / "stable"
    install.mkdir()
    monkeypatch.setenv("UNREAD_HOME", str(install))

    from unread.secrets_backend import _reset_keychain_service_cache, keychain_service

    _reset_keychain_service_cache()
    first = keychain_service()
    second = keychain_service()
    assert first == second


def test_keychain_service_does_not_use_bare_name(monkeypatch, tmp_path: Path) -> None:
    """Even default installs must NOT resolve to the bare `"unread"`
    service — that's the namespace any other Python process can fetch
    out of the user's keychain."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.secrets_backend import _reset_keychain_service_cache, keychain_service

    _reset_keychain_service_cache()
    name = keychain_service()
    assert name != "unread"
    assert ":" in name


# ---------- read / write / delete use the namespaced name ----------------


def test_keychain_write_uses_namespaced_service(monkeypatch, tmp_path: Path, fake_keyring) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))

    from unread.secrets_backend import _reset_keychain_service_cache, keychain_service, keychain_write

    _reset_keychain_service_cache()
    service = keychain_service()
    assert keychain_write("openai.api_key", "sk-namespaced-write") is True
    assert fake_keyring.store.get((service, "openai.api_key")) == "sk-namespaced-write"
    # And NOT under the bare legacy name.
    assert fake_keyring.store.get(("unread", "openai.api_key")) is None


def test_keychain_read_uses_namespaced_service(monkeypatch, tmp_path: Path, fake_keyring) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))

    from unread.secrets_backend import _reset_keychain_service_cache, keychain_read, keychain_service

    _reset_keychain_service_cache()
    service = keychain_service()
    fake_keyring.store[(service, "telegram.api_hash")] = "hash-from-namespaced"
    assert keychain_read("telegram.api_hash") == "hash-from-namespaced"


def test_keychain_delete_uses_namespaced_service(monkeypatch, tmp_path: Path, fake_keyring) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))

    from unread.secrets_backend import (
        _reset_keychain_service_cache,
        keychain_delete,
        keychain_service,
    )

    _reset_keychain_service_cache()
    service = keychain_service()
    fake_keyring.store[(service, "google.api_key")] = "delete-me"
    assert keychain_delete("google.api_key") is True
    assert (service, "google.api_key") not in fake_keyring.store


# ---------- migration shim -----------------------------------------------


def test_legacy_value_migrated_forward_on_read(monkeypatch, tmp_path: Path, fake_keyring) -> None:
    """Legacy entries under the bare `"unread"` service get forward-ported
    AND the legacy entry is deleted on first namespaced read."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))

    from unread.secrets_backend import _reset_keychain_service_cache, keychain_read, keychain_service

    _reset_keychain_service_cache()
    service = keychain_service()
    # Seed a legacy entry as if a previous release wrote it.
    fake_keyring.store[("unread", "openai.api_key")] = "sk-legacy"

    # First read: should return the legacy value AND migrate it.
    assert keychain_read("openai.api_key") == "sk-legacy"
    # The value now lives under the namespaced service.
    assert fake_keyring.store.get((service, "openai.api_key")) == "sk-legacy"
    # The legacy entry has been deleted.
    assert ("unread", "openai.api_key") not in fake_keyring.store

    # Subsequent reads short-circuit to the namespaced slot.
    assert keychain_read("openai.api_key") == "sk-legacy"


def test_legacy_migration_only_runs_on_namespaced_miss(monkeypatch, tmp_path: Path, fake_keyring) -> None:
    """When the namespaced slot already has a value, the legacy slot
    is NOT consulted — avoids accidentally overwriting a fresh write
    with a stale legacy value."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))

    from unread.secrets_backend import _reset_keychain_service_cache, keychain_read, keychain_service

    _reset_keychain_service_cache()
    service = keychain_service()
    # Both slots populated; namespaced wins.
    fake_keyring.store[(service, "openai.api_key")] = "sk-fresh"
    fake_keyring.store[("unread", "openai.api_key")] = "sk-stale-legacy"

    assert keychain_read("openai.api_key") == "sk-fresh"
    # Legacy entry must remain untouched (no opportunistic deletion).
    assert fake_keyring.store.get(("unread", "openai.api_key")) == "sk-stale-legacy"


def test_legacy_migration_noop_when_neither_slot_set(monkeypatch, tmp_path: Path, fake_keyring) -> None:
    """Cold cache + neither namespaced nor legacy entry → returns None."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))

    from unread.secrets_backend import _reset_keychain_service_cache, keychain_read

    _reset_keychain_service_cache()
    assert keychain_read("openai.api_key") is None


# ---------- back-compat shim for `KEYCHAIN_SERVICE` import --------------


def test_legacy_constant_import_returns_namespaced_name(monkeypatch, tmp_path: Path) -> None:
    """Old code that did `from unread.secrets_backend import KEYCHAIN_SERVICE`
    keeps working — the lazy `__getattr__` shim returns the live
    namespaced value."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.secrets_backend import _reset_keychain_service_cache

    _reset_keychain_service_cache()
    from unread.secrets_backend import KEYCHAIN_SERVICE, keychain_service

    assert keychain_service() == KEYCHAIN_SERVICE
    assert KEYCHAIN_SERVICE.startswith("unread:")
