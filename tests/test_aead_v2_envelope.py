"""Tests for the v2 (`$u2$`) AEAD envelope with slot-name binding.

Pre-prod review #2: v1 envelopes had `associated_data=None` so a
copy-paste of the ciphertext from `openai.api_key` into
`telegram.api_hash` decrypted cleanly. v2 binds the slot name as AAD
so a slot swap fires `InvalidTag`.

These tests pin:
  * Round-trip encrypt(slot_name=...) → decrypt(slot_name=...) works.
  * Decrypt with the wrong slot_name fails.
  * Legacy v1 ciphertexts continue to decrypt (back-compat).
  * `envelope_version` returns the right tag.
"""

from __future__ import annotations

import pytest

from unread.security.crypto import (
    ENCRYPTED_PREFIX,
    ENCRYPTED_PREFIX_V2,
    PassphraseError,
    decrypt,
    decrypt_with_key,
    derive_key,
    encrypt,
    encrypt_with_key,
    envelope_version,
    parse_envelope,
)


def test_encrypt_with_slot_emits_v2():
    blob = encrypt("the-secret", "the-passphrase", slot_name="openai.api_key")
    assert blob.startswith(ENCRYPTED_PREFIX_V2)
    assert envelope_version(blob) == 2


def test_encrypt_without_slot_emits_v1_for_backcompat():
    blob = encrypt("the-secret", "the-passphrase")
    assert blob.startswith(ENCRYPTED_PREFIX)
    assert envelope_version(blob) == 1


def test_v2_round_trip_with_matching_slot():
    blob = encrypt("the-secret", "pp", slot_name="openai.api_key")
    plaintext = decrypt(blob, "pp", slot_name="openai.api_key")
    assert plaintext == "the-secret"


def test_v2_decrypt_with_wrong_slot_raises():
    """The whole point of v2 — a copy-paste of the openai ciphertext
    into the telegram.api_hash row must fail AEAD verify."""
    blob = encrypt("openai-key-value", "pp", slot_name="openai.api_key")
    with pytest.raises(PassphraseError):
        decrypt(blob, "pp", slot_name="telegram.api_hash")


def test_v2_decrypt_with_no_slot_raises():
    """Failing to pass slot_name on a v2 blob is a programmer bug —
    the AEAD verify fires because no AAD is provided where some was
    expected."""
    blob = encrypt("v", "pp", slot_name="openai.api_key")
    with pytest.raises(PassphraseError):
        decrypt(blob, "pp")


def test_v1_round_trip_still_works():
    """Legacy `$u1$` blobs (no slot binding) keep decrypting unchanged
    so existing installs aren't broken by the upgrade."""
    blob = encrypt("legacy-secret", "pp")
    assert blob.startswith(ENCRYPTED_PREFIX)
    plaintext = decrypt(blob, "pp")
    assert plaintext == "legacy-secret"


def test_v1_ignores_slot_name():
    """Reading a v1 blob with `slot_name=...` is harmless — the
    decrypt path passes `associated_data=None` regardless because the
    legacy envelope was written without AAD."""
    blob = encrypt("v1-value", "pp")
    plaintext = decrypt(blob, "pp", slot_name="openai.api_key")
    assert plaintext == "v1-value"


def test_with_key_path_also_supports_v2():
    """The amortized-Scrypt path used by `_persist_upgrade` must also
    emit v2 when `slot_name` is supplied."""
    salt = b"\x01" * 16
    key = derive_key("pp", salt)
    blob = encrypt_with_key("plain", key, salt=salt, slot_name="openai.api_key")
    assert blob.startswith(ENCRYPTED_PREFIX_V2)
    assert decrypt_with_key(blob, key, slot_name="openai.api_key") == "plain"
    with pytest.raises(PassphraseError):
        decrypt_with_key(blob, key, slot_name="anthropic.api_key")


def test_v2_swap_with_v1_fallback_disallowed():
    """Cannot strip the `$u2$` prefix and pretend it's `$u1$`. Body
    layout is identical so an attacker might try; but the AEAD verify
    still fires because the original encrypt used slot-name AAD."""
    blob = encrypt("v", "pp", slot_name="openai.api_key")
    forged_v1 = ENCRYPTED_PREFIX + blob[len(ENCRYPTED_PREFIX_V2) :]
    # parse_envelope succeeds (same body shape) but decrypt fires.
    parse_envelope(forged_v1)
    with pytest.raises(PassphraseError):
        decrypt(forged_v1, "pp")
