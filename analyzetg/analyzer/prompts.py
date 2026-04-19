"""Analysis presets (spec §9.1).

`prompt_version` is part of the cache key — bumping it invalidates stale results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
    options_keys: list[str] = field(default_factory=list)

    def render_user(self, **kw: object) -> str:
        return self.user_template.format(**kw)


_SYSTEM_SUMMARY = (
    "Ты — аналитик Telegram-чатов. Твоя задача — выделять ключевые темы, тезисы "
    "и настроение обсуждения. Пиши по-русски, кратко, без воды. Не выдумывай факты."
)

_SYSTEM_ACTION = (
    "Ты извлекаешь конкретные действия и задачи из переписки: кто что должен сделать, "
    "к какому сроку, что именно решено. Пиши списком, по-русски. Если задач нет — "
    "так и скажи одной строкой."
)

_SYSTEM_DIGEST = (
    "Ты составляешь короткий дайджест обсуждения: 5–10 самых важных тем, 1–2 строки на "
    "каждую, по-русски. Опускай мелкий шум и повторы."
)

_SYSTEM_DECISIONS = (
    "Ты выделяешь принятые решения из обсуждения. Пиши списком: решение — "
    "кто принял — когда — обоснование (если явно). По-русски."
)

_USER_TEMPLATE_COMMON = (
    "Период: {period}\n"
    "Чат: {title}\n"
    "Сообщений: {msg_count}\n"
    "---\n"
    "{messages}"
)


PRESETS: dict[str, Preset] = {
    "summary": Preset(
        name="summary",
        prompt_version="v1",
        system=_SYSTEM_SUMMARY,
        user_template="Задача: сделай структурированное саммари за указанный период "
        "(5–10 тезисов, по 1–2 строки каждый; выдели топ-3 темы в начале).\n\n"
        + _USER_TEMPLATE_COMMON,
        needs_reduce=True,
        filter_model="gpt-5.4-nano",
        final_model="gpt-5.4",
        output_budget_tokens=1500,
    ),
    "action_items": Preset(
        name="action_items",
        prompt_version="v1",
        system=_SYSTEM_ACTION,
        user_template="Задача: вынеси конкретные action items (кто/что/когда). "
        "В конце — краткая итоговая таблица в markdown.\n\n" + _USER_TEMPLATE_COMMON,
        needs_reduce=True,
        filter_model="gpt-5.4-nano",
        final_model="gpt-5.4",
        output_budget_tokens=1200,
    ),
    "digest": Preset(
        name="digest",
        prompt_version="v1",
        system=_SYSTEM_DIGEST,
        user_template="Задача: составь дайджест обсуждения за период.\n\n"
        + _USER_TEMPLATE_COMMON,
        needs_reduce=True,
        filter_model="gpt-5.4-nano",
        final_model="gpt-5.4",
        output_budget_tokens=1200,
    ),
    "decisions": Preset(
        name="decisions",
        prompt_version="v1",
        system=_SYSTEM_DECISIONS,
        user_template="Задача: перечисли принятые решения.\n\n" + _USER_TEMPLATE_COMMON,
        needs_reduce=True,
        filter_model="gpt-5.4-nano",
        final_model="gpt-5.4",
        output_budget_tokens=1000,
    ),
}


REDUCE_PROMPT = (
    "Ниже — несколько уже готовых мини-саммари одного и того же чата, полученных из "
    "разных фрагментов переписки. Слей их в одно финальное саммари в заданном формате. "
    "Не дублируй пункты, объединяй похожие, сохраняй фактологию. Пиши по-русски."
)


def load_custom_preset(prompt_file: Path) -> Preset:
    """Load a custom preset from a markdown file.

    File format (simple): first paragraph → system, everything after the line
    `---USER---` → user_template (must contain {messages}, {period}, {title}, {msg_count}).
    If the marker is absent, the entire file is used as the user instruction header and
    a default system prompt is applied.
    """
    text = prompt_file.read_text(encoding="utf-8")
    if "---USER---" in text:
        system, user = text.split("---USER---", 1)
        system = system.strip()
        user_instr = user.strip()
    else:
        system = (
            "Ты аналитик Telegram-чата. Следуй инструкциям ниже и отвечай по-русски, "
            "без воды, опираясь только на приведённые сообщения."
        )
        user_instr = text.strip()

    if "{messages}" not in user_instr:
        user_instr += "\n\n" + _USER_TEMPLATE_COMMON

    # Ensure all needed placeholders
    for key in ("{period}", "{title}", "{msg_count}", "{messages}"):
        if key not in user_instr:
            user_instr += "\n" + key
    # Stable content hash → stable prompt_version
    import hashlib

    version = "custom-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return Preset(
        name="custom",
        prompt_version=version,
        system=system,
        user_template=user_instr,
        needs_reduce=True,
        filter_model="gpt-5.4-nano",
        final_model="gpt-5.4",
        output_budget_tokens=1500,
    )
