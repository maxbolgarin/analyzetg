"""i18n.t lookup table — round-trip, fallback, and language defaults."""

from __future__ import annotations

import pytest

from atg import i18n
from atg.config import get_settings, reset_settings


def test_t_returns_explicit_lang():
    assert i18n.t("period_label", "en") == "Period"
    assert i18n.t("period_label", "ru") == "Период"


def test_t_falls_back_to_english_for_missing_lang():
    # `period_label` has no German entry → the lookup falls through to EN.
    assert i18n.t("period_label", "de") == "Period"


def test_t_unknown_key_raises():
    with pytest.raises(KeyError, match="unknown key"):
        i18n.t("does_not_exist", "en")


def test_language_name_known_codes():
    assert i18n.language_name("en") == "English"
    assert i18n.language_name("ru") == "Russian"
    # Unknown codes degrade to a Title-cased version.
    assert i18n.language_name("xx") == "Xx"
    assert i18n.language_name("") == "English"


def test_t_resolves_active_locale_when_lang_omitted(monkeypatch):
    # `t("...")` with no lang reads from settings.locale.language at
    # call time — not at import time — so test monkeypatches take effect.
    reset_settings()
    s = get_settings()
    s.locale.language = "ru"
    try:
        assert i18n.t("period_label") == "Период"
        s.locale.language = "en"
        assert i18n.t("period_label") == "Period"
    finally:
        reset_settings()
