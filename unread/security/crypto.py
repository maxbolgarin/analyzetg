"""Passphrase-derived encryption for the `passphrase` secrets backend.

Encrypts the values of the allowlisted credentials and the Telethon
session string with ChaCha20Poly1305 under a Scrypt-derived key. The
passphrase itself is never written to disk; only a per-install salt
and the per-record nonce / ciphertext are persisted.

Storage layout (per-record, base64-encoded as a single string):

    "$u1$" || b64(salt[16] || nonce[12] || ciphertext+tag)

The ``$u1$`` prefix lets `secrets.read_secrets` cheaply tell ciphertext
from plaintext when the active backend changes mid-flight (e.g. after
``unread security downgrade`` but before the user's settings reload).

Key cache:
* In-process: the derived key sits in module-level memory once a
  passphrase has been entered, so subsequent decrypts in the same
  command don't re-Scrypt.
* Cross-invocation: optional opt-in via :func:`unlock` writes the key
  to ``$XDG_RUNTIME_DIR/unread/key`` (Linux tmpfs that wipes on
  reboot) or ``~/.unread/.runtime/key`` (macOS / fallback) with mode
  0o600. :func:`lock` deletes it. :func:`load_cached_key` honours an
  optional wall-clock expiry stamped alongside the key bytes.

Failure surface — never log or print the passphrase / key. Wrong
passphrase decrypt raises :class:`PassphraseError`; the caller turns
that into a friendly "passphrase didn't match — try again" line.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as _stdsecrets
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from unread.util.logging import get_logger

log = get_logger(__name__)


# Format / parameters --------------------------------------------------------

ENCRYPTED_PREFIX = "$u1$"
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12

# Scrypt parameters. ``n=2**17`` lands at roughly 100 ms on a modern
# laptop CPU (target: visible but tolerable). Tunable in the future
# via ``app_settings::security.kdf_cost`` if we ever need to ramp it.
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1


class PassphraseError(RuntimeError):
    """Raised when the supplied passphrase fails to decrypt the payload."""


class NotEncryptedError(RuntimeError):
    """Raised when ``decrypt`` is called on a string without the format prefix."""


@dataclass(frozen=True)
class CryptoEnvelope:
    """Parsed view of one encrypted record."""

    salt: bytes
    nonce: bytes
    ciphertext: bytes


def _b64encode(data: bytes) -> str:
    # urlsafe variant, no padding — keeps the encoded value friendly
    # to env files / shell echoing without breaking on `=` characters.
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(ENCRYPTED_PREFIX)


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Scrypt(passphrase, salt) → 32-byte key. ~100 ms on modern hardware."""
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt must be {SALT_LEN} bytes")
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


# Backward-compat alias kept private for any internal call sites that
# might have imported the underscore name.
_derive_key = derive_key


def encrypt(plaintext: str, passphrase: str) -> str:
    """Encrypt ``plaintext`` under a key derived from ``passphrase``.

    Each call generates a fresh salt + nonce so re-encrypting the same
    value produces a different ciphertext. That defeats simple "did
    this row change" diffs against a backup but doesn't matter for
    correctness — the `secrets` table key (`openai.api_key` etc.)
    already tells the reader which slot it is.
    """
    if not plaintext:
        # Encrypting an empty string is a smell — empty values are
        # treated as "slot not set" everywhere else in the codebase.
        raise ValueError("refuse to encrypt an empty string")
    salt = _stdsecrets.token_bytes(SALT_LEN)
    nonce = _stdsecrets.token_bytes(NONCE_LEN)
    key = _derive_key(passphrase, salt)
    aead = ChaCha20Poly1305(key)
    ct = aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    blob = salt + nonce + ct
    return f"{ENCRYPTED_PREFIX}{_b64encode(blob)}"


def encrypt_with_key(plaintext: str, key: bytes, salt: bytes | None = None) -> str:
    """Variant that reuses a precomputed key (skip the Scrypt step).

    Used by the migration commands (`upgrade`, `rotate-passphrase`)
    where we encrypt many slots in one go and want amortized cost.
    Caller is responsible for keeping the matching salt around if
    they want decrypt-with-key to work without a re-derivation.
    """
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes")
    if salt is None:
        salt = _stdsecrets.token_bytes(SALT_LEN)
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt must be {SALT_LEN} bytes")
    nonce = _stdsecrets.token_bytes(NONCE_LEN)
    aead = ChaCha20Poly1305(key)
    ct = aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    blob = salt + nonce + ct
    return f"{ENCRYPTED_PREFIX}{_b64encode(blob)}"


def parse_envelope(ciphertext: str) -> CryptoEnvelope:
    """Strip the prefix, decode base64, split into (salt, nonce, ciphertext)."""
    if not is_encrypted(ciphertext):
        raise NotEncryptedError("missing $u1$ prefix; not an encrypted record")
    body = _b64decode(ciphertext[len(ENCRYPTED_PREFIX) :])
    if len(body) < SALT_LEN + NONCE_LEN + 16:  # 16 = AEAD tag minimum
        raise PassphraseError("ciphertext is too short to be a valid record")
    salt = body[:SALT_LEN]
    nonce = body[SALT_LEN : SALT_LEN + NONCE_LEN]
    ct = body[SALT_LEN + NONCE_LEN :]
    return CryptoEnvelope(salt=salt, nonce=nonce, ciphertext=ct)


def decrypt(ciphertext: str, passphrase: str) -> str:
    """Decrypt a single ``$u1$``-prefixed record. Wrong passphrase → ``PassphraseError``."""
    env = parse_envelope(ciphertext)
    key = _derive_key(passphrase, env.salt)
    aead = ChaCha20Poly1305(key)
    try:
        plaintext = aead.decrypt(env.nonce, env.ciphertext, associated_data=None)
    except InvalidTag as e:
        raise PassphraseError("passphrase didn't decrypt") from e
    return plaintext.decode("utf-8")


def decrypt_with_key(ciphertext: str, key: bytes) -> str:
    """Decrypt with a precomputed (salt-bound) key.

    Only correct when the salt baked into the ciphertext matches the
    salt used to derive ``key``. Used by `read_secrets` after a
    passphrase has been entered once: the reader derives one key per
    distinct salt and reuses it across slots that share that salt
    (the common case — `upgrade` writes all slots with the same salt).
    """
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes")
    env = parse_envelope(ciphertext)
    aead = ChaCha20Poly1305(key)
    try:
        plaintext = aead.decrypt(env.nonce, env.ciphertext, associated_data=None)
    except InvalidTag as e:
        raise PassphraseError("key didn't decrypt") from e
    return plaintext.decode("utf-8")


# Key cache ------------------------------------------------------------------

# In-process key store. Lives only in module memory; never persisted.
_PROCESS_KEYS: dict[bytes, bytes] = {}


def remember_key_for_salt(salt: bytes, key: bytes) -> None:
    """Cache ``key`` keyed by ``salt`` so subsequent slot decrypts are free."""
    if len(key) != KEY_LEN or len(salt) != SALT_LEN:
        raise ValueError("size mismatch for cached key/salt")
    _PROCESS_KEYS[salt] = key


def lookup_key_for_salt(salt: bytes) -> bytes | None:
    return _PROCESS_KEYS.get(salt)


def forget_process_keys() -> None:
    _PROCESS_KEYS.clear()


# Cross-invocation cache ------------------------------------------------------


def _runtime_dir() -> Path:
    """Pick the best ephemeral directory for the cached key.

    Order:
      1. ``$XDG_RUNTIME_DIR/unread/`` — Linux tmpfs that wipes on reboot.
         Owned by the user, mode 0o700 by systemd default.
      2. ``~/.unread/.runtime/`` — macOS and Linux fallback.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidate = Path(xdg) / "unread"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            os.chmod(candidate, 0o700)
            return candidate
        except OSError:
            pass

    from unread.core.paths import unread_home
    from unread.util.fsmode import ensure_private_dir

    return ensure_private_dir(unread_home() / ".runtime")


def _cache_path() -> Path:
    return _runtime_dir() / "key"


def store_cached_key(key: bytes, salt: bytes, ttl_seconds: int | None) -> Path:
    """Write the derived key to the runtime dir for cross-invocation reuse.

    Stored as JSON ``{"v": 1, "expires_at": <epoch|None>, "salt_b64":
    "...", "key_b64": "..."}`` so a future format bump is safe and a
    user inspecting the file knows what it is. Mode 0o600 from
    creation via :func:`secret_write_text`.
    """
    from unread.util.fsmode import secret_write_text

    if len(key) != KEY_LEN or len(salt) != SALT_LEN:
        raise ValueError("size mismatch for cached key/salt")

    expires_at = None if ttl_seconds is None else int(time.time()) + int(ttl_seconds)
    payload = json.dumps(
        {
            "v": 1,
            "expires_at": expires_at,
            "salt_b64": _b64encode(salt),
            "key_b64": _b64encode(key),
        }
    )
    path = _cache_path()
    secret_write_text(path, payload)
    return path


def load_cached_key() -> tuple[bytes, bytes] | None:
    """Return ``(salt, key)`` from the runtime cache, or None if missing/expired."""
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("crypto.cache_unreadable", err=str(e)[:200])
        return None
    expires_at = payload.get("expires_at")
    if expires_at is not None and time.time() >= float(expires_at):
        # Expired — wipe it on read so a stale cache doesn't linger.
        import contextlib as _cl

        with _cl.suppress(OSError):
            path.unlink(missing_ok=True)
        return None
    try:
        salt = _b64decode(payload["salt_b64"])
        key = _b64decode(payload["key_b64"])
    except (KeyError, ValueError) as e:
        log.warning("crypto.cache_malformed", err=str(e)[:200])
        return None
    if len(salt) != SALT_LEN or len(key) != KEY_LEN:
        return None
    return salt, key


def forget_cached_key() -> bool:
    """Delete the runtime cache file. Returns True iff a file was removed."""
    path = _cache_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning("crypto.cache_unlink_failed", err=str(e)[:200])
        return False


# Settings keys --------------------------------------------------------------


# Stored in `app_settings`. The salt for the install lives here so a
# fresh process can derive the matching key without first reading a
# secret. (Each ciphertext also embeds the salt for self-containment;
# the standalone copy is what `unlock` uses to skip Scrypt.)
APP_SETTING_SALT = "security.kdf_salt"


__all__ = [
    "APP_SETTING_SALT",
    "ENCRYPTED_PREFIX",
    "KEY_LEN",
    "NONCE_LEN",
    "SALT_LEN",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "NotEncryptedError",
    "PassphraseError",
    "decrypt",
    "decrypt_with_key",
    "derive_key",
    "encrypt",
    "encrypt_with_key",
    "forget_cached_key",
    "forget_process_keys",
    "is_encrypted",
    "load_cached_key",
    "lookup_key_for_salt",
    "parse_envelope",
    "remember_key_for_salt",
    "store_cached_key",
]
