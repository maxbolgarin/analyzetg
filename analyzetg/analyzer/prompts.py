"""Analysis presets (spec §9.1).

Presets live as markdown files in `<project>/presets/*.md`. Each file has a
YAML-ish frontmatter block with metadata (name, prompt_version, models,
output budget) and a body split by the `---USER---` marker: everything
before it is the system prompt, everything after is the user template.

`prompt_version` is part of the cache key — bump it to invalidate stale
results when you edit a preset.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

PRESETS_DIR = Path(__file__).resolve().parent.parent.parent / "presets"
USER_MARKER = "---USER---"
DEFAULT_USER_TAIL = "Период: {period}\nЧат: {title}\nСообщений: {msg_count}\n---\n{messages}"


@dataclass(slots=True)
class Preset:
    name: str
    prompt_version: str
    system: str
    user_template: str
    needs_reduce: bool = True
    filter_model: str = "gpt-5.4-nano"
    final_model: str = "gpt-5.4"
    output_budget_tokens: int = 1500
    # Per-chunk output cap in the map phase. Kept separate from
    # `output_budget_tokens` (which governs the final reduce output) so
    # individual chunks can produce richer mini-summaries without inflating
    # the final answer budget.
    map_output_tokens: int = 1500
    options_keys: list[str] = field(default_factory=list)

    def render_user(self, **kw: object) -> str:
        return self.user_template.format(**kw)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split `---\\n...\\n---\\n<body>` into (meta, body). No external deps."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    block = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


def _coerce_bool(v: str) -> bool:
    return v.lower() in ("true", "yes", "1", "on")


def _load_preset_file(path: Path) -> Preset:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)

    if USER_MARKER in body:
        system, user = body.split(USER_MARKER, 1)
        system = system.strip()
        user_template = user.strip()
    else:
        system = body.strip()
        user_template = DEFAULT_USER_TAIL

    # Ensure the user template carries all placeholders the pipeline expects.
    for key in ("{period}", "{title}", "{msg_count}", "{messages}"):
        if key not in user_template:
            user_template = user_template + "\n" + key

    name = meta.get("name") or path.stem
    return Preset(
        name=name,
        prompt_version=meta.get("prompt_version", "v1"),
        system=system,
        user_template=user_template,
        needs_reduce=_coerce_bool(meta.get("needs_reduce", "true")),
        filter_model=meta.get("filter_model", "gpt-5.4-nano"),
        final_model=meta.get("final_model", "gpt-5.4"),
        output_budget_tokens=int(meta.get("output_budget_tokens", "1500")),
        map_output_tokens=int(meta.get("map_output_tokens", "1500")),
    )


def _load_all_presets() -> dict[str, Preset]:
    if not PRESETS_DIR.is_dir():
        raise RuntimeError(
            f"Presets directory not found: {PRESETS_DIR}. "
            "Check out the repo or create it with at least summary.md inside."
        )
    out: dict[str, Preset] = {}
    for md in sorted(PRESETS_DIR.glob("*.md")):
        # Underscore-prefixed files are internal helpers (e.g. _reduce.md).
        if md.stem.startswith("_") or md.stem.lower() == "readme":
            continue
        preset = _load_preset_file(md)
        out[preset.name] = preset
    return out


def _load_reduce_prompt() -> str:
    path = PRESETS_DIR / "_reduce.md"
    if not path.is_file():
        # Sensible fallback so the app keeps working if the file is deleted.
        return (
            "Ниже — несколько уже готовых мини-саммари одного и того же чата. "
            "Слей их в одно финальное саммари. Не дублируй пункты, объединяй похожие. "
            "Пиши по-русски."
        )
    return path.read_text(encoding="utf-8").strip()


PRESETS: dict[str, Preset] = _load_all_presets()
REDUCE_PROMPT: str = _load_reduce_prompt()


def load_custom_preset(prompt_file: Path) -> Preset:
    """Load an ad-hoc preset from a markdown file.

    Same format as the bundled presets: optional YAML frontmatter, body split
    by `---USER---`. Without frontmatter, a default system prompt is used and
    the whole body becomes the user instruction header.
    """
    text = prompt_file.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    if USER_MARKER in body:
        system, user = body.split(USER_MARKER, 1)
        system = system.strip()
        user_instr = user.strip()
    elif meta:
        system = body.strip()
        user_instr = DEFAULT_USER_TAIL
    else:
        system = (
            "Ты аналитик Telegram-чата. Следуй инструкциям ниже и отвечай по-русски, "
            "без воды, опираясь только на приведённые сообщения."
        )
        user_instr = text.strip()

    if "{messages}" not in user_instr:
        user_instr += "\n\n" + DEFAULT_USER_TAIL

    for key in ("{period}", "{title}", "{msg_count}", "{messages}"):
        if key not in user_instr:
            user_instr += "\n" + key

    version = meta.get("prompt_version") or "custom-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return Preset(
        name=meta.get("name", "custom"),
        prompt_version=version,
        system=system,
        user_template=user_instr,
        needs_reduce=_coerce_bool(meta.get("needs_reduce", "true")),
        filter_model=meta.get("filter_model", "gpt-5.4-nano"),
        final_model=meta.get("final_model", "gpt-5.4"),
        output_budget_tokens=int(meta.get("output_budget_tokens", "1500")),
        map_output_tokens=int(meta.get("map_output_tokens", "1500")),
    )
