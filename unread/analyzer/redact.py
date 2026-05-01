"""PII redaction applied to the LLM-bound prompt only.

Use case: a user wants summaries of a chat that occasionally contains
phone numbers, emails, IBANs, or credit-card-shaped digits, but does
not want that data to leave their machine via the OpenAI / Anthropic /
Google API. The DB row, the saved Markdown report, and the user's own
view stay untouched — only the prompt the LLM sees is redacted.

Toggled via `--redact` (CLI) or `analyze.redact = true` (config). Kept
intentionally simple: regex-based, opt-in, deterministic. The match
patterns prefer false negatives over false positives — we'd rather let
an unusual phone shape through than chew up a transaction-id every
time it looks numeric.

Returns the redacted text plus a per-kind hit count. The hit count is
surfaced in the run summary so users can see what was scrubbed without
us logging the raw matches. Don't log the matches anywhere — redacted
prompts that get logged via OpenAI's API still benefit from the user's
own redaction, but the local logger should not undo it.
"""

from __future__ import annotations

import re

# All matchers compile once at import time — they're applied per
# message body during a run with -r in the thousands.

# Email: RFC-5322-simplified. We don't need the full grammar; this
# catches >99% of mailbox-shapes while ignoring the long tail (quoted
# locals, IP-literals) that almost no real chat contains.
_EMAIL_RE = re.compile(
    r"""
    \b
    [A-Za-z0-9._%+\-]+      # local-part: word/punct
    @
    [A-Za-z0-9.\-]+         # domain
    \.[A-Za-z]{2,24}        # TLD
    \b
    """,
    re.VERBOSE,
)

# Phone number: E.164 (+CC then 7-14 digits with optional separators).
# Word-boundary on both sides so we don't chew through long digit runs
# like SHA hashes or order numbers. The leading `+` is required —
# detecting "phones" without country code is unreliable enough that
# false positives outweigh the safety win.
_PHONE_RE = re.compile(
    r"""
    (?<![\w+\-])             # left edge: not part of a longer token
    \+\d{1,3}                # country code: +1 to +999
    [\s\-.()]*               # optional separators
    (?:\d[\s\-.()]*){6,14}   # 7–15 digits with optional separators
    (?![\d])                 # right edge: not followed by another digit
    """,
    re.VERBOSE,
)

# IBAN: 2-letter country code + 2 check digits + up to 30 alphanum.
# Excludes confusing matches by requiring a trailing word boundary
# AND that the candidate is at least 15 chars long.
_IBAN_RE = re.compile(
    r"""
    \b
    [A-Z]{2}\d{2}            # country + check digits
    [A-Z0-9]{11,30}          # BBAN (basic bank account number)
    \b
    """,
    re.VERBOSE,
)

# Credit card: 13-19 digits, optionally separated by spaces or dashes.
# Validated with Luhn so we don't redact every order-id-shaped run.
_CARD_CANDIDATE_RE = re.compile(
    r"""
    (?<![\w])                # left edge
    (?:\d[ \-]?){12,18}\d    # 13–19 digit groups
    (?![\d])                 # right edge: no digit after
    """,
    re.VERBOSE,
)


def _luhn_check(digits: str) -> bool:
    """Return True if `digits` (only 0-9) passes the Luhn checksum.

    The Luhn algorithm is what every card scheme uses to validate the
    final check digit. False matches without it would redact most
    16-digit numbers — order numbers, transaction ids, etc.
    """
    if not (13 <= len(digits) <= 19):
        return False
    s = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48  # ord('0') == 48
        if d < 0 or d > 9:
            return False
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return s % 10 == 0


def _replace_card(match: re.Match[str]) -> str:
    candidate = match.group(0)
    digits = "".join(c for c in candidate if c.isdigit())
    if _luhn_check(digits):
        return "[redacted-card]"
    return candidate


def redact(
    text: str,
    *,
    kinds: set[str] | None = None,
) -> tuple[str, dict[str, int]]:
    """Scrub PII from `text` and return (redacted, per-kind counts).

    `kinds` selects which families to redact. Default is the full set
    `{"phone", "email", "iban", "card"}`. An unknown kind name is a
    no-op (lets future kinds be added without crashing old callers).
    `counts` is a dict keyed by the same kind strings; absent kinds
    have no entry.
    """
    if not text:
        return text, {}
    enabled = kinds or {"phone", "email", "iban", "card"}
    counts: dict[str, int] = {}

    if "email" in enabled:
        text, n = _EMAIL_RE.subn("[redacted-email]", text)
        if n:
            counts["email"] = n

    if "phone" in enabled:
        text, n = _PHONE_RE.subn("[redacted-phone]", text)
        if n:
            counts["phone"] = n

    if "iban" in enabled:
        text, n = _IBAN_RE.subn("[redacted-iban]", text)
        if n:
            counts["iban"] = n

    if "card" in enabled:
        # Luhn-validated — most digit-runs come back unchanged.
        new_text, _matches = _CARD_CANDIDATE_RE.subn(_replace_card, text)
        # Count by re-scanning the result; cheaper than threading state.
        n_redacted = new_text.count("[redacted-card]") - text.count("[redacted-card]")
        if n_redacted > 0:
            counts["card"] = n_redacted
        text = new_text

    return text, counts


def total_hits(counts: dict[str, int]) -> int:
    """Sum across kind counts. Convenience for log summaries."""
    return sum(counts.values())
