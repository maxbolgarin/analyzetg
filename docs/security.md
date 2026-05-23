# Security and privacy

← Back to [README](../README.md)

`unread` stores three classes of high-value data on disk: API keys
(OpenAI, Anthropic, Google, OpenRouter, Telegram api_id/api_hash), the
Telegram session (full-account auth — anyone with this file can log
in as you), and the cached chat content (messages, transcripts,
analysis reports). The defenses below address the realistic threats:
other users on the same machine, backup leakage (Time Machine /
iCloud / Dropbox / NAS), stolen disks without FDE, and Telegram
session theft.

## File permissions (always on)

`~/.unread/` is created mode `0o700` and every file written inside
(`data.sqlite`, `session.sqlite`, reports, `.env`) is tightened to
`0o600` immediately after creation. The `media/` and runtime cache
directories are also `0o700`. Verify with `unread doctor` — it flags
overpermissive modes, warns when the install lives under a known
cloud-sync folder (iCloud Drive, Dropbox, OneDrive, Google Drive),
and reports FileVault / LUKS state.

`unread`'s structured logger has an API-key redactor: anything
matching `sk-…`, `sk-ant-…`, `sk-or-…`, `AIza…`, `gsk_…`, or known
secret-shaped event-dict keys (`api_key`, `api_hash`, `passphrase`,
`session_string`, `auth_key`, …) gets masked before rendering. So
even if a debug session is shared, raw credentials don't leak.

## Three storage backends — `unread security`

The credential-storage backend is one-shot switchable:

```bash
unread security status               # active backend, slot inventory, FDE check
unread security set plain            # plaintext on disk (default)
unread security set keystore         # OS keychain (recommended)
unread security set pass             # passphrase-encrypted (strongest)
unread security set plain            # … and back, any direction
```

| Backend | Storage | Defends against | UX |
|---|---|---|---|
| `plain` | `~/.unread/storage/data.sqlite` (plaintext) | Other local users (via `0o700`/`0o600`) | Zero friction |
| `keystore` | macOS Keychain / Linux Secret Service / Windows Credential Manager | Other local users + backup leakage (Keychain isn't backed up) | Zero friction — unlocked with your login |
| `pass` | Same DB, but every value encrypted with a key derived from your passphrase. The Telegram session moves into an encrypted Telethon `StringSession` and the on-disk `session.sqlite[.session]` file disappears entirely. | All of the above + stolen disk without FDE + VPS host operator + Telegram session theft from a backup | Passphrase prompt; cache it for the shell session via `unread security unlock` |

### `keystore` — the recommended default for personal machines

`unread security set keystore` migrates every saved API key into the
OS-native keychain. Verify on macOS with:

```bash
security find-generic-password -s unread -a openai.api_key
sqlite3 ~/.unread/storage/data.sqlite "SELECT key, length(value) FROM secrets"
# → DB rows are blank; values live in Keychain under service "unread"
```

No passphrase is ever asked — the keychain is unlocked when you log
in. Keychain content is encrypted at rest with a key bound to your
user account. Backups (Time Machine, iCloud) by default exclude the
Keychain database, so a leaked Time Machine snapshot of `~/.unread/`
no longer contains your API keys. On Linux, `keystore` requires a
running Secret Service (`gnome-keyring` / KWallet); on headless
hosts the wizard skips this offer silently and you stay on `plain`.

### `pass` — passphrase-encrypted, strongest at-rest guarantee

`unread security set pass` runs an interactive prompt: pick a
passphrase, the CLI runs `Scrypt` (n=2¹⁷, ~100 ms) to derive a key,
and re-encrypts every secret value plus the Telegram session string
under `ChaCha20Poly1305`. The plaintext `session.sqlite[.session]`
file is removed at the end — there's nothing on disk an attacker
can copy to impersonate you on Telegram, even from a backup.

**On every command** that reads encrypted secrets, the key is
sourced in this order: in-process cache → `UNREAD_PASSPHRASE` env
var (handy for cron / CI) → `getpass()` prompt (TTY only). To skip
the prompt across invocations:

```bash
unread security unlock              # cache the derived key until you `lock`
unread security unlock --keep 30m   # … or for a bounded TTL
unread tg chats run                 # no prompt
unread security lock                # wipe the cache now
unread security rotate-passphrase   # change the passphrase
```

The cached key lives at `$XDG_RUNTIME_DIR/unread/key` on Linux
(tmpfs — auto-cleared on reboot) or `~/.unread/.runtime/key` on
macOS / fallback. Mode is `0o600` from creation. The passphrase
itself is **never** persisted — only the derived key, only when you
explicitly `unlock`.

What encrypted mode does NOT defend against: malware running as
your user (same UID can read decrypted process memory regardless),
or a coerced-passphrase attack. For both, the mitigation is at the
OS level (FileVault, app sandboxing, hardware tokens), not at
`unread`'s layer.

## Telegram session hygiene

```bash
unread security revoke-session
```

Removes the local Telethon session file and prints a reminder to
revoke remotely from Telegram → Settings → Devices → Active Sessions.
Doing both is the only way to fully invalidate a leaked session.

## Quick recommendations

- **Personal Mac / Windows machine:** `unread security set keystore`. Zero friction, defends backup leakage, fits the realistic threat model.
- **VPS / shared host / paranoid laptop with no FDE:** `unread security set pass`, optionally `unread security unlock --keep 1h` per shell.
- **Headless Linux / Docker / CI:** stay on `plain`, set `UNREAD_PASSPHRASE` only if you've also enabled `pass` mode and need automation.
- **Anywhere:** turn on FileVault / LUKS, exclude `~/.unread/` from `tmutil`/cloud sync, run `unread doctor` after first setup.

## Privacy: PII redaction before the LLM

`--redact` (or `analyze.redact = true` in config) scrubs PII from the
text sent to the LLM provider, while keeping originals in the local
DB and the saved Markdown report. Only the API payload is redacted.

```bash
unread @somegroup --redact
```

Patterns scrubbed: phone numbers (E.164 with `+` prefix), emails,
IBANs, and Luhn-valid credit-card numbers. Each match is replaced
with `[redacted-phone]` / `[redacted-email]` / `[redacted-iban]` /
`[redacted-card]`, and the run summary shows per-kind counts so you
can see what was filtered. Caching honors the flag — toggling
`--redact` produces a different cache row, so you never serve a
non-redacted cached result on a redacted run (or vice versa).

The match is intentionally conservative (regex with strict word
boundaries) to keep false positives low. SHA hashes and order-id
numerics are not flagged; non-E.164 phone shapes (raw 10-digit US
numbers without `+1` prefix) pass through. If you need stricter
redaction, layer your own preset prompt that asks the LLM to
generalize personal references — `--redact` complements that, it
doesn't replace it.
