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

# v1 envelope (legacy): `$u1$ || b64(salt[16] || nonce[12] || ct+tag)`,
# encrypted with AEAD `associated_data=None`. Vulnerable to slot-swap:
# attacker who can edit the DB (or a buggy migration) can move the
# ciphertext from one secrets slot into another and the AEAD still
# verifies because nothing binds the ciphertext to its slot name.
ENCRYPTED_PREFIX = "$u1$"

# v2 envelope: same body layout, but the AEAD `associated_data` carries
# `unread:v2:<slot_name>` so the ciphertext is cryptographically bound
# to its slot. A swap from `openai.api_key` into `telegram.api_hash`
# now fails `InvalidTag` on read instead of silently decrypting.
# Reads accept both prefixes.
ENCRYPTED_PREFIX_V2 = "$u2$"
_AAD_V2_PREFIX = b"unread:v2:"

# v3 envelope: same body layout as v2, but the AEAD `associated_data`
# also folds in the salt and nonce framing. A tampered envelope where
# the salt or nonce was swapped now fails `InvalidTag` even when the
# attacker computed a matching v2 AAD; the framing bytes are part of
# the integrity check, not just inputs to KDF / cipher. New writes
# always use v3 when a `slot_name` is supplied. Reads accept v1, v2,
# and v3; v1/v2 rows auto-migrate to v3 on first successful decrypt
# (see `unread/secrets.py:_persist_*_rewrites_sync`).
ENCRYPTED_PREFIX_V3 = "$u3$"
_AAD_V3_PREFIX = b"unread:v3:"

KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12

# Scrypt parameters. ``n=2**18`` lands at roughly 200 ms on a modern
# laptop CPU — at the upper end of "perceptible but tolerable" for an
# unlock prompt and the modern (2026) recommendation for a high-value
# target with multiple LLM keys + a Telegram session in scope.
# Tunable in the future via ``app_settings::security.kdf_cost`` if we
# ever need to ramp it further.
#
# `UNREAD_SCRYPT_N` env override: the test suite sets this to a much
# smaller value (e.g. 2**10 ~ 5 ms) so encrypt/decrypt round-trips don't
# burn 200 ms x N per file. Anything below 2**14 is unsafe for production
# and we explicitly clamp to the production floor when the var is unset
# or invalid. NOT documented for end users — production installs should
# never touch this.
PRODUCTION_SCRYPT_N = 2**18
try:
    _override = os.environ.get("UNREAD_SCRYPT_N")
    SCRYPT_N = int(_override) if _override else PRODUCTION_SCRYPT_N
except ValueError:
    SCRYPT_N = PRODUCTION_SCRYPT_N
SCRYPT_R = 8
SCRYPT_P = 1

# Default TTL for the cross-invocation key cache. The pre-prod review
# flagged "no TTL" (== ttl_seconds=None) as a security regression: on
# macOS / Windows the cache file lives on persistent disk, so a
# never-expiring entry survives reboot and gets included in the user's
# ~/ backup. 30 minutes is enough for a single CLI session of multiple
# unread invocations without forcing a re-prompt mid-flow, and short
# enough that an idle machine isn't carrying the master key forever.
DEFAULT_KEY_CACHE_TTL_SEC = 30 * 60


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


_B64_URLSAFE_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")


def _b64decode(text: str) -> bytes:
    """Strict urlsafe base64 decode. Raises on stray non-base64 chars.

    `urlsafe_b64decode` doesn't accept `validate=` (stdlib quirk), so
    we hand-check the alphabet before decoding. A malformed envelope
    errors at parse time instead of silently producing partial bytes
    that later trip InvalidTag on the AEAD verify and waste a Scrypt.
    """
    if not all(c in _B64_URLSAFE_ALPHABET for c in text):
        raise PassphraseError("malformed base64 in encrypted record: stray non-alphabet char")
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (ValueError, UnicodeEncodeError) as e:
        raise PassphraseError(f"malformed base64 in encrypted record: {e}") from e


def is_encrypted(value: str | None) -> bool:
    return bool(value) and (
        value.startswith(ENCRYPTED_PREFIX)
        or value.startswith(ENCRYPTED_PREFIX_V2)
        or value.startswith(ENCRYPTED_PREFIX_V3)
    )


def envelope_version(value: str | None) -> int:
    """Return 1 for `$u1$`, 2 for `$u2$`, 3 for `$u3$`, 0 for plaintext / unknown."""
    if not value:
        return 0
    if value.startswith(ENCRYPTED_PREFIX_V3):
        return 3
    if value.startswith(ENCRYPTED_PREFIX_V2):
        return 2
    if value.startswith(ENCRYPTED_PREFIX):
        return 1
    return 0


def _aad_for(slot_name: str | None) -> bytes | None:
    """AEAD additional-data binding for the v2 envelope.

    None means "v1 / no slot binding" — kept so legacy readers of
    `$u1$` blobs verify against the original AAD-less ciphertext.
    """
    if not slot_name:
        return None
    return _AAD_V2_PREFIX + slot_name.encode("utf-8")


def _aad_for_v3(slot_name: str, salt: bytes, nonce: bytes) -> bytes:
    """AEAD additional-data binding for the v3 envelope.

    Includes the slot name (defends against slot-swap, like v2) AND
    the salt + nonce framing (defends against on-disk tampering of
    the framing bytes). A reader who alters just the salt before the
    base64 boundary now trips ``InvalidTag`` instead of slipping past
    the parse layer to the AEAD verify only because the tag still
    happens to verify under the wrong-key derivation.
    """
    return _AAD_V3_PREFIX + slot_name.encode("utf-8") + salt + nonce


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Scrypt(passphrase, salt) → 32-byte key. ~100 ms on modern hardware."""
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt must be {SALT_LEN} bytes")
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


# Backward-compat alias kept private for any internal call sites that
# might have imported the underscore name.
_derive_key = derive_key


def encrypt(plaintext: str, passphrase: str, *, slot_name: str | None = None) -> str:
    """Encrypt ``plaintext`` under a key derived from ``passphrase``.

    Each call generates a fresh salt + nonce so re-encrypting the same
    value produces a different ciphertext.

    `slot_name` opts into the v3 (`$u3$`) envelope: the slot name plus
    the salt + nonce framing are bound as AEAD `associated_data` so
    both a copy-paste of the ciphertext into a different slot AND a
    tamper of the framing bytes fail `InvalidTag`. Writers that know
    which slot they're targeting (`put_secrets`, the session-string
    write path, `_persist_upgrade`) always pass it. Legacy callers
    that don't pass `slot_name` keep emitting `$u1$` for backward compat.
    """
    if not plaintext:
        # Encrypting an empty string is a smell — empty values are
        # treated as "slot not set" everywhere else in the codebase.
        raise ValueError("refuse to encrypt an empty string")
    salt = _stdsecrets.token_bytes(SALT_LEN)
    nonce = _stdsecrets.token_bytes(NONCE_LEN)
    key = _derive_key(passphrase, salt)
    aead = ChaCha20Poly1305(key)
    if slot_name:
        aad = _aad_for_v3(slot_name, salt, nonce)
        prefix = ENCRYPTED_PREFIX_V3
    else:
        aad = None
        prefix = ENCRYPTED_PREFIX
    ct = aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data=aad)
    blob = salt + nonce + ct
    return f"{prefix}{_b64encode(blob)}"


def encrypt_with_key(
    plaintext: str,
    key: bytes,
    salt: bytes | None = None,
    *,
    slot_name: str | None = None,
) -> str:
    """Variant that reuses a precomputed key (skip the Scrypt step).

    Used by the migration commands (`upgrade`, `rotate-passphrase`)
    where we encrypt many slots in one go and want amortized cost.
    Caller is responsible for keeping the matching salt around if
    they want decrypt-with-key to work without a re-derivation.
    `slot_name` enables the v3 envelope (see :func:`encrypt`).
    """
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes")
    if salt is None:
        salt = _stdsecrets.token_bytes(SALT_LEN)
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt must be {SALT_LEN} bytes")
    nonce = _stdsecrets.token_bytes(NONCE_LEN)
    aead = ChaCha20Poly1305(key)
    if slot_name:
        aad = _aad_for_v3(slot_name, salt, nonce)
        prefix = ENCRYPTED_PREFIX_V3
    else:
        aad = None
        prefix = ENCRYPTED_PREFIX
    ct = aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data=aad)
    blob = salt + nonce + ct
    return f"{prefix}{_b64encode(blob)}"


def parse_envelope(ciphertext: str) -> CryptoEnvelope:
    """Strip the prefix, decode base64, split into (salt, nonce, ciphertext).

    Accepts the legacy `$u1$` envelope (no slot binding), the v2
    `$u2$` envelope (slot bound via AEAD `associated_data`), and the
    v3 `$u3$` envelope (slot + salt + nonce all bound via AEAD AAD).
    The body layout is identical across all three versions; the
    version flag lives in the prefix and the AAD requirement is
    enforced by the caller.
    """
    if not is_encrypted(ciphertext):
        raise NotEncryptedError("missing $u1$/$u2$/$u3$ prefix; not an encrypted record")
    if ciphertext.startswith(ENCRYPTED_PREFIX_V3):
        body = _b64decode(ciphertext[len(ENCRYPTED_PREFIX_V3) :])
    elif ciphertext.startswith(ENCRYPTED_PREFIX_V2):
        body = _b64decode(ciphertext[len(ENCRYPTED_PREFIX_V2) :])
    else:
        body = _b64decode(ciphertext[len(ENCRYPTED_PREFIX) :])
    if len(body) < SALT_LEN + NONCE_LEN + 16:  # 16 = AEAD tag minimum
        raise PassphraseError("ciphertext is too short to be a valid record")
    salt = body[:SALT_LEN]
    nonce = body[SALT_LEN : SALT_LEN + NONCE_LEN]
    ct = body[SALT_LEN + NONCE_LEN :]
    return CryptoEnvelope(salt=salt, nonce=nonce, ciphertext=ct)


def _aad_for_envelope(ciphertext: str, env: CryptoEnvelope, slot_name: str | None) -> bytes | None:
    """Resolve the AAD that the AEAD verify expects for `ciphertext`.

    Centralizes the per-version branching so `decrypt` and
    `decrypt_with_key` stay in lockstep. v1 envelopes ignore
    `slot_name` (no binding existed), v2 binds slot only, v3 binds
    slot + salt + nonce framing.
    """
    if ciphertext.startswith(ENCRYPTED_PREFIX_V3):
        if not slot_name:
            # v3 always binds the slot. A caller that forgot to pass
            # one would silently fail with InvalidTag — but the real
            # bug is upstream, so surface it explicitly.
            raise PassphraseError("v3 envelope requires slot_name on decrypt")
        return _aad_for_v3(slot_name, env.salt, env.nonce)
    if ciphertext.startswith(ENCRYPTED_PREFIX_V2):
        return _aad_for(slot_name)
    return None


def decrypt(ciphertext: str, passphrase: str, *, slot_name: str | None = None) -> str:
    """Decrypt a single `$u1$`/`$u2$`/`$u3$`-prefixed record.

    Wrong passphrase → ``PassphraseError``. For `$u2$` and `$u3$`
    envelopes the caller MUST supply the matching `slot_name`; v3
    additionally binds the salt + nonce framing into the AEAD AAD, so
    on-disk tampering of those bytes also raises ``PassphraseError``.
    Reading `$u1$` ignores `slot_name` for back-compat — the legacy
    envelope has no slot binding to verify against.
    """
    env = parse_envelope(ciphertext)
    key = _derive_key(passphrase, env.salt)
    aead = ChaCha20Poly1305(key)
    aad = _aad_for_envelope(ciphertext, env, slot_name)
    try:
        plaintext = aead.decrypt(env.nonce, env.ciphertext, associated_data=aad)
    except InvalidTag as e:
        raise PassphraseError("passphrase didn't decrypt") from e
    return plaintext.decode("utf-8")


def migrate_v1_to_v2_with_key(ciphertext: str, key: bytes, *, slot_name: str) -> str:
    """Re-encrypt a v1 (`$u1$`) blob as v2 with slot-bound AAD.

    Kept for back-compat with any external caller that hard-coded
    "migrate to v2"; new code should call :func:`migrate_to_v3_with_key`
    which targets the current envelope version.

    Salt is preserved across the migration. Refuses already-v2 blobs.
    """
    if not slot_name:
        raise ValueError("migrate_v1_to_v2_with_key requires a non-empty slot_name")
    if not is_encrypted(ciphertext):
        raise ValueError("migrate_v1_to_v2_with_key: input is not encrypted")
    if ciphertext.startswith(ENCRYPTED_PREFIX_V2) or ciphertext.startswith(ENCRYPTED_PREFIX_V3):
        raise ValueError("migrate_v1_to_v2_with_key: input is already v2 or newer")
    plaintext = decrypt_with_key(ciphertext, key)
    env = parse_envelope(ciphertext)
    # Force v2 (not v3) so the back-compat semantics stay literal.
    nonce = _stdsecrets.token_bytes(NONCE_LEN)
    aead = ChaCha20Poly1305(key)
    aad = _aad_for(slot_name)
    ct = aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data=aad)
    blob = env.salt + nonce + ct
    return f"{ENCRYPTED_PREFIX_V2}{_b64encode(blob)}"


def migrate_to_v3_with_key(ciphertext: str, key: bytes, *, slot_name: str) -> str:
    """Re-encrypt a v1 or v2 blob as v3 with slot + framing-bound AAD.

    The v3 envelope folds the salt and nonce into the AEAD AAD so any
    on-disk tamper of the framing trips ``InvalidTag`` instead of
    being noticed only by the underlying AEAD verify. New writes use
    v3; this helper performs the one-shot rewrite for installs that
    still carry v1 / v2 rows from before the upgrade.

    Salt is preserved so the cached-key-by-salt machinery
    (`_PROCESS_KEYS`) keeps amortizing Scrypt across slots that shared
    a salt. Only the nonce + ciphertext + AAD change.

    Refuses already-v3 blobs and plaintext.
    """
    if not slot_name:
        raise ValueError("migrate_to_v3_with_key requires a non-empty slot_name")
    if not is_encrypted(ciphertext):
        raise ValueError("migrate_to_v3_with_key: input is not encrypted")
    if ciphertext.startswith(ENCRYPTED_PREFIX_V3):
        raise ValueError("migrate_to_v3_with_key: input is already v3")
    plaintext = decrypt_with_key(ciphertext, key, slot_name=slot_name)
    env = parse_envelope(ciphertext)
    return encrypt_with_key(plaintext, key, salt=env.salt, slot_name=slot_name)


def decrypt_with_key(ciphertext: str, key: bytes, *, slot_name: str | None = None) -> str:
    """Decrypt with a precomputed (salt-bound) key.

    Only correct when the salt baked into the ciphertext matches the
    salt used to derive ``key``. Used by `read_secrets` after a
    passphrase has been entered once: the reader derives one key per
    distinct salt and reuses it across slots that share that salt
    (the common case — `upgrade` writes all slots with the same salt).

    For `$u2$` and `$u3$` envelopes, `slot_name` MUST match the slot
    the ciphertext was originally written under, otherwise the AEAD
    verify fires.
    """
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes")
    env = parse_envelope(ciphertext)
    aead = ChaCha20Poly1305(key)
    aad = _aad_for_envelope(ciphertext, env, slot_name)
    try:
        plaintext = aead.decrypt(env.nonce, env.ciphertext, associated_data=aad)
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


# Public alias — `killme` and other consumers used to reach in via
# `_cache_path` with `# type: ignore[attr-defined]`. The private name
# is retained for backwards compatibility with internal callers; new
# code should use `runtime_key_cache_path` so a rename of the helper
# doesn't silently break the killme cleanup.
def runtime_key_cache_path() -> Path:
    """Return the absolute path of the cross-invocation key cache file."""
    return _cache_path()


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
    "DEFAULT_KEY_CACHE_TTL_SEC",
    "ENCRYPTED_PREFIX",
    "ENCRYPTED_PREFIX_V2",
    "ENCRYPTED_PREFIX_V3",
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
    "envelope_version",
    "forget_cached_key",
    "forget_process_keys",
    "is_encrypted",
    "load_cached_key",
    "lookup_key_for_salt",
    "migrate_to_v3_with_key",
    "migrate_v1_to_v2_with_key",
    "parse_envelope",
    "remember_key_for_salt",
    "runtime_key_cache_path",
    "store_cached_key",
]
