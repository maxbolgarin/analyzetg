"""Tests for the slot-bound AEAD envelopes.

Three envelope versions exist:
  - ``$u1$``: legacy, no AAD binding. Slot swap and framing tamper
    both undetected pre-AEAD.
  - ``$u2$``: AAD = ``"unread:v2:" + slot_name``. Slot swap fires.
  - ``$u3$``: AAD = ``"unread:v3:" + slot_name + salt + nonce``. Slot
    swap *and* framing tamper both fire. New writes always emit v3.

These tests pin the slot-binding semantics for v2 and v3 simultaneously
(v3 is a strict superset of v2's slot-binding guarantee).
"""

from __future__ import annotations

import pytest

from unread.security.crypto import (
    ENCRYPTED_PREFIX,
    ENCRYPTED_PREFIX_V2,
    ENCRYPTED_PREFIX_V3,
    PassphraseError,
    decrypt,
    decrypt_with_key,
    derive_key,
    encrypt,
    encrypt_with_key,
    envelope_version,
    parse_envelope,
)


def test_encrypt_with_slot_emits_v3():
    blob = encrypt("the-secret", "the-passphrase", slot_name="openai.api_key")
    assert blob.startswith(ENCRYPTED_PREFIX_V3)
    assert envelope_version(blob) == 3


def test_encrypt_without_slot_emits_v1_for_backcompat():
    blob = encrypt("the-secret", "the-passphrase")
    assert blob.startswith(ENCRYPTED_PREFIX)
    assert envelope_version(blob) == 1


def test_v3_round_trip_with_matching_slot():
    blob = encrypt("the-secret", "pp", slot_name="openai.api_key")
    plaintext = decrypt(blob, "pp", slot_name="openai.api_key")
    assert plaintext == "the-secret"


def test_v3_decrypt_with_wrong_slot_raises():
    """A copy-paste of the openai ciphertext into the
    telegram.api_hash row must fail AEAD verify."""
    blob = encrypt("openai-key-value", "pp", slot_name="openai.api_key")
    with pytest.raises(PassphraseError):
        decrypt(blob, "pp", slot_name="telegram.api_hash")


def test_v3_decrypt_with_no_slot_raises():
    """Failing to pass slot_name on a v3 blob is a programmer bug —
    surfaced explicitly so the failure mode names the misuse."""
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


def test_with_key_path_also_supports_v3():
    """The amortized-Scrypt path used by `_persist_upgrade` must also
    emit v3 when `slot_name` is supplied."""
    salt = b"\x01" * 16
    key = derive_key("pp", salt)
    blob = encrypt_with_key("plain", key, salt=salt, slot_name="openai.api_key")
    assert blob.startswith(ENCRYPTED_PREFIX_V3)
    assert decrypt_with_key(blob, key, slot_name="openai.api_key") == "plain"
    with pytest.raises(PassphraseError):
        decrypt_with_key(blob, key, slot_name="anthropic.api_key")


def test_v3_swap_with_v1_fallback_disallowed():
    """Cannot strip the `$u3$` prefix and pretend it's `$u1$`. Body
    layout is identical so an attacker might try; but the AEAD verify
    still fires because the original encrypt used slot+framing AAD."""
    blob = encrypt("v", "pp", slot_name="openai.api_key")
    forged_v1 = ENCRYPTED_PREFIX + blob[len(ENCRYPTED_PREFIX_V3) :]
    # parse_envelope succeeds (same body shape) but decrypt fires.
    parse_envelope(forged_v1)
    with pytest.raises(PassphraseError):
        decrypt(forged_v1, "pp")


def test_v3_swap_with_v2_prefix_disallowed():
    """A v3 blob retagged as v2 must also fail: the v2 AAD is
    framing-free, so it differs from what was used at encrypt time."""
    blob = encrypt("v", "pp", slot_name="openai.api_key")
    forged_v2 = ENCRYPTED_PREFIX_V2 + blob[len(ENCRYPTED_PREFIX_V3) :]
    with pytest.raises(PassphraseError):
        decrypt(forged_v2, "pp", slot_name="openai.api_key")
