"""ISO 639-1 catalog and helpers.

Three data structures live here:

* :data:`POPULAR_LANGUAGES` — ordered shortlist shown in the settings
  picker by default. ~24 languages chosen for LLM coverage and global
  reach.
* :data:`ISO_639_1` — the full standard mapping (code -> English name).
  Used to validate the picker's "Custom code..." entry and the matching
  CLI flags. Hand-rolled to avoid a runtime dep on babel / pycountry.
* :data:`WHISPER_LANGUAGES` — the subset OpenAI Whisper documents as
  supported. Used to warn (not block) when the user sets an audio hint
  outside this set.

Public API:

* :func:`normalize_language_code` — accepts a code, an English name,
  or a code with locale suffix (e.g. ``pt-BR``); returns the canonical
  lowercase 2-letter code or ``None`` if not recognised.
* :func:`is_valid_language_code` — convenience boolean wrapper.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Popular shortlist
# ---------------------------------------------------------------------------

# Ordered for the picker: en + ru first (i18n-supported), then by global
# speaker counts and LLM-coverage. Each tuple is (code, English name).
POPULAR_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("ru", "Russian"),
    ("zh", "Chinese"),
    ("es", "Spanish"),
    ("hi", "Hindi"),
    ("ar", "Arabic"),
    ("pt", "Portuguese"),
    ("fr", "French"),
    ("de", "German"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("it", "Italian"),
    ("tr", "Turkish"),
    ("pl", "Polish"),
    ("uk", "Ukrainian"),
    ("nl", "Dutch"),
    ("vi", "Vietnamese"),
    ("th", "Thai"),
    ("id", "Indonesian"),
    ("he", "Hebrew"),
    ("cs", "Czech"),
    ("sv", "Swedish"),
    ("el", "Greek"),
    ("fa", "Persian"),
)

POPULAR_CODES: tuple[str, ...] = tuple(code for code, _ in POPULAR_LANGUAGES)

# ---------------------------------------------------------------------------
# Whisper-supported languages (documented at platform.openai.com)
# ---------------------------------------------------------------------------

WHISPER_LANGUAGES: frozenset[str] = frozenset(
    {
        "af",
        "ar",
        "az",
        "be",
        "bg",
        "bs",
        "ca",
        "cs",
        "cy",
        "da",
        "de",
        "el",
        "en",
        "es",
        "et",
        "fa",
        "fi",
        "fr",
        "gl",
        "he",
        "hi",
        "hr",
        "hu",
        "hy",
        "id",
        "is",
        "it",
        "ja",
        "kk",
        "kn",
        "ko",
        "lt",
        "lv",
        "mi",
        "mk",
        "mr",
        "ms",
        "ne",
        "nl",
        "no",
        "pl",
        "pt",
        "ro",
        "ru",
        "sk",
        "sl",
        "sr",
        "sv",
        "sw",
        "ta",
        "th",
        "tl",
        "tr",
        "uk",
        "ur",
        "vi",
        "zh",
    }
)

# ---------------------------------------------------------------------------
# Full ISO 639-1
# ---------------------------------------------------------------------------

_ISO_BLOCK_A = {
    "aa": "Afar",
    "ab": "Abkhazian",
    "ae": "Avestan",
    "af": "Afrikaans",
    "ak": "Akan",
    "am": "Amharic",
    "an": "Aragonese",
    "ar": "Arabic",
    "as": "Assamese",
    "av": "Avaric",
    "ay": "Aymara",
    "az": "Azerbaijani",
    "ba": "Bashkir",
    "be": "Belarusian",
    "bg": "Bulgarian",
    "bh": "Bihari",
    "bi": "Bislama",
    "bm": "Bambara",
    "bn": "Bengali",
    "bo": "Tibetan",
    "br": "Breton",
    "bs": "Bosnian",
}

_ISO_BLOCK_C = {
    "ca": "Catalan",
    "ce": "Chechen",
    "ch": "Chamorro",
    "co": "Corsican",
    "cr": "Cree",
    "cs": "Czech",
    "cu": "Church Slavic",
    "cv": "Chuvash",
    "cy": "Welsh",
}

_ISO_BLOCK_D = {
    "da": "Danish",
    "de": "German",
    "dv": "Divehi",
    "dz": "Dzongkha",
}

_ISO_BLOCK_E = {
    "ee": "Ewe",
    "el": "Greek",
    "en": "English",
    "eo": "Esperanto",
    "es": "Spanish",
    "et": "Estonian",
    "eu": "Basque",
}

_ISO_BLOCK_F = {
    "fa": "Persian",
    "ff": "Fula",
    "fi": "Finnish",
    "fj": "Fijian",
    "fo": "Faroese",
    "fr": "French",
    "fy": "Western Frisian",
}

_ISO_BLOCK_G = {
    "ga": "Irish",
    "gd": "Scottish Gaelic",
    "gl": "Galician",
    "gn": "Guarani",
    "gu": "Gujarati",
    "gv": "Manx",
}

_ISO_BLOCK_H = {
    "ha": "Hausa",
    "he": "Hebrew",
    "hi": "Hindi",
    "ho": "Hiri Motu",
    "hr": "Croatian",
    "ht": "Haitian Creole",
    "hu": "Hungarian",
    "hy": "Armenian",
    "hz": "Herero",
}

_ISO_BLOCK_I = {
    "ia": "Interlingua",
    "id": "Indonesian",
    "ie": "Interlingue",
    "ig": "Igbo",
    "ii": "Sichuan Yi",
    "ik": "Inupiaq",
    "io": "Ido",
    "is": "Icelandic",
    "it": "Italian",
    "iu": "Inuktitut",
}

_ISO_BLOCK_J = {
    "ja": "Japanese",
    "jv": "Javanese",
}

_ISO_BLOCK_K = {
    "ka": "Georgian",
    "kg": "Kongo",
    "ki": "Kikuyu",
    "kj": "Kuanyama",
    "kk": "Kazakh",
    "kl": "Kalaallisut",
    "km": "Khmer",
    "kn": "Kannada",
    "ko": "Korean",
    "kr": "Kanuri",
    "ks": "Kashmiri",
    "ku": "Kurdish",
    "kv": "Komi",
    "kw": "Cornish",
    "ky": "Kyrgyz",
}

_ISO_BLOCK_L = {
    "la": "Latin",
    "lb": "Luxembourgish",
    "lg": "Ganda",
    "li": "Limburgish",
    "ln": "Lingala",
    "lo": "Lao",
    "lt": "Lithuanian",
    "lu": "Luba-Katanga",
    "lv": "Latvian",
}

_ISO_BLOCK_M = {
    "mg": "Malagasy",
    "mh": "Marshallese",
    "mi": "Maori",
    "mk": "Macedonian",
    "ml": "Malayalam",
    "mn": "Mongolian",
    "mr": "Marathi",
    "ms": "Malay",
    "mt": "Maltese",
    "my": "Burmese",
}

_ISO_BLOCK_N = {
    "na": "Nauru",
    "nb": "Norwegian Bokmal",
    "nd": "North Ndebele",
    "ne": "Nepali",
    "ng": "Ndonga",
    "nl": "Dutch",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian",
    "nr": "South Ndebele",
    "nv": "Navajo",
    "ny": "Chichewa",
}

_ISO_BLOCK_O = {
    "oc": "Occitan",
    "oj": "Ojibwe",
    "om": "Oromo",
    "or": "Odia",
    "os": "Ossetian",
}

_ISO_BLOCK_P = {
    "pa": "Punjabi",
    "pi": "Pali",
    "pl": "Polish",
    "ps": "Pashto",
    "pt": "Portuguese",
}

_ISO_BLOCK_Q = {
    "qu": "Quechua",
}

_ISO_BLOCK_R = {
    "rm": "Romansh",
    "rn": "Rundi",
    "ro": "Romanian",
    "ru": "Russian",
    "rw": "Kinyarwanda",
}

_ISO_BLOCK_S = {
    "sa": "Sanskrit",
    "sc": "Sardinian",
    "sd": "Sindhi",
    "se": "Northern Sami",
    "sg": "Sango",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sm": "Samoan",
    "sn": "Shona",
    "so": "Somali",
    "sq": "Albanian",
    "sr": "Serbian",
    "ss": "Swati",
    "st": "Southern Sotho",
    "su": "Sundanese",
    "sv": "Swedish",
    "sw": "Swahili",
}

_ISO_BLOCK_T = {
    "ta": "Tamil",
    "te": "Telugu",
    "tg": "Tajik",
    "th": "Thai",
    "ti": "Tigrinya",
    "tk": "Turkmen",
    "tl": "Tagalog",
    "tn": "Tswana",
    "to": "Tongan",
    "tr": "Turkish",
    "ts": "Tsonga",
    "tt": "Tatar",
    "tw": "Twi",
    "ty": "Tahitian",
}

_ISO_BLOCK_U = {
    "ug": "Uyghur",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
}

_ISO_BLOCK_V = {
    "ve": "Venda",
    "vi": "Vietnamese",
    "vo": "Volapuk",
}

_ISO_BLOCK_W = {
    "wa": "Walloon",
    "wo": "Wolof",
}

_ISO_BLOCK_X = {
    "xh": "Xhosa",
}

_ISO_BLOCK_Y = {
    "yi": "Yiddish",
    "yo": "Yoruba",
}

_ISO_BLOCK_Z = {
    "za": "Zhuang",
    "zh": "Chinese",
    "zu": "Zulu",
}

ISO_639_1: dict[str, str] = {}
for _block in (
    _ISO_BLOCK_A,
    _ISO_BLOCK_C,
    _ISO_BLOCK_D,
    _ISO_BLOCK_E,
    _ISO_BLOCK_F,
    _ISO_BLOCK_G,
    _ISO_BLOCK_H,
    _ISO_BLOCK_I,
    _ISO_BLOCK_J,
    _ISO_BLOCK_K,
    _ISO_BLOCK_L,
    _ISO_BLOCK_M,
    _ISO_BLOCK_N,
    _ISO_BLOCK_O,
    _ISO_BLOCK_P,
    _ISO_BLOCK_Q,
    _ISO_BLOCK_R,
    _ISO_BLOCK_S,
    _ISO_BLOCK_T,
    _ISO_BLOCK_U,
    _ISO_BLOCK_V,
    _ISO_BLOCK_W,
    _ISO_BLOCK_X,
    _ISO_BLOCK_Y,
    _ISO_BLOCK_Z,
):
    ISO_639_1.update(_block)


# Reverse lookup for "user typed an English name" — case-insensitive. Built
# once at import; tiny.
_NAME_TO_CODE: dict[str, str] = {name.lower(): code for code, name in ISO_639_1.items()}


def normalize_language_code(raw: str) -> str | None:
    """Return canonical lowercase ISO 639-1 code, or None if not recognised.

    Accepts:

    * 2-letter code (case-insensitive), e.g. ``"PT"`` -> ``"pt"``.
    * Code with locale suffix, e.g. ``"pt-BR"`` -> ``"pt"``,
      ``"zh_Hans"`` -> ``"zh"``.
    * English name (case-insensitive), e.g. ``"Portuguese"`` -> ``"pt"``,
      ``"  scottish gaelic "`` -> ``"gd"``.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    # Locale-tagged form: take the part before the first separator.
    head = s
    for sep in ("-", "_", "."):
        if sep in head:
            head = head.split(sep, 1)[0]
            break

    # 2-letter code path.
    if len(head) == 2 and head.isalpha():
        code = head.lower()
        if code in ISO_639_1:
            return code
        return None

    # English-name path. Try the original (no locale stripping) first
    # since names like "Scottish Gaelic" contain a space, not a separator.
    name_key = s.lower()
    if name_key in _NAME_TO_CODE:
        return _NAME_TO_CODE[name_key]
    return None


def is_valid_language_code(raw: str) -> bool:
    """True iff :func:`normalize_language_code` would return non-None."""
    return normalize_language_code(raw) is not None


def language_display_name(code: str) -> str:
    """English display name for an ISO 639-1 code, or title-cased fallback."""
    if not code:
        return ""
    return ISO_639_1.get(code.lower(), code.title())
