"""AI structuring and speech-to-text services.

The text model uses an OpenAI-compatible endpoint. The default points to Groq,
while ``AI_BASE_URL``/``AI_MODEL`` can target another compatible provider.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, BinaryIO

from openai import AsyncOpenAI

from config import settings
from prompts import STRUCTURING_SYSTEM_PROMPT

MODES = {
    "auto": "Автоматически выбери структуру, подходящую содержанию.",
    "project": "Оформи как проектный план: цель, контекст, этапы, риски и следующие действия.",
    "meeting": "Оформи как итоги встречи: контекст, обсуждение, решения, задачи и вопросы.",
    "content": "Оформи как контентный замысел: тезис, аудитория, смысловые блоки и план создания.",
    "personal": "Оформи как личную заметку: наблюдения, выводы, намерения и вопросы.",
}
_structure_slots = asyncio.Semaphore(3)
_transcription_slots = asyncio.Semaphore(2)


class AIConfigurationError(RuntimeError):
    """Raised when a required AI provider credential is not configured."""


class AIResponseError(RuntimeError):
    """Raised when the model returns an unusable response."""


def _client(api_key: str, base_url: str) -> AsyncOpenAI:
    if not api_key:
        raise AIConfigurationError(
            "AI API key is not configured. Set GROQ_API_KEY or AI_API_KEY."
        )
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_structure(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider output into the stable application contract."""
    raw_actions = value.get("actions")
    actions: list[dict[str, str]] = []
    if isinstance(raw_actions, list):
        for item in raw_actions:
            if isinstance(item, str) and item.strip():
                actions.append({"task": item.strip(), "owner": "", "deadline": ""})
            elif isinstance(item, dict) and str(item.get("task", "")).strip():
                actions.append(
                    {
                        "task": str(item.get("task", "")).strip(),
                        "owner": str(item.get("owner", "") or "").strip(),
                        "deadline": str(item.get("deadline", "") or "").strip(),
                    }
                )

    title = str(value.get("title") or "Структурированная мысль").strip()
    summary = str(value.get("summary") or "").strip()
    structured_text = str(value.get("structured_text") or "").strip()
    if not structured_text:
        raise AIResponseError("The model returned an empty structured document.")

    return {
        "title": title[:240],
        "summary": summary,
        "structured_text": structured_text,
        "decisions": _as_string_list(value.get("decisions")),
        "actions": actions,
        "ideas": _as_string_list(value.get("ideas")),
        "open_questions": _as_string_list(value.get("open_questions")),
        "tags": _as_string_list(value.get("tags"))[:10],
    }


def parse_structure_response(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIResponseError("The model returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise AIResponseError("The model response must be a JSON object.")
    return normalize_structure(value)


async def structure_thoughts(source_text: str, mode: str = "auto") -> dict[str, Any]:
    selected_mode = mode if mode in MODES else "auto"
    prompt = (
        f"Режим документа: {selected_mode}. {MODES[selected_mode]}\n\n"
        "Ниже исходная запись. Следуй критическим правилам и ничего не выдумывай.\n\n"
        f"<source>\n{source_text}\n</source>"
    )
    async with _structure_slots:
        response = await _client(
            settings.ai_api_key, settings.ai_base_url
        ).chat.completions.create(
            model=settings.ai_model,
            messages=[
                {"role": "system", "content": STRUCTURING_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=6000,
            response_format={"type": "json_object"},
        )
    content = response.choices[0].message.content
    if not content:
        raise AIResponseError("The model returned an empty response.")
    return parse_structure_response(content)


async def transcribe_audio(
    audio: bytes | BinaryIO,
    *,
    filename: str = "recording.webm",
    content_type: str = "audio/webm",
    language: str | None = None,
) -> str:
    client = _client(settings.transcription_api_key, settings.transcription_base_url)
    arguments: dict[str, Any] = {
        "file": (filename, audio, content_type),
        "model": settings.transcription_model,
        "response_format": "json",
        "temperature": 0,
    }
    if language and re.fullmatch(r"[a-z]{2}", language.lower()):
        arguments["language"] = language.lower()
    async with _transcription_slots:
        response = await client.audio.transcriptions.create(**arguments)
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        raise AIResponseError("Speech recognition returned an empty transcript.")
    return text


def render_markdown(structured: dict[str, Any], source_text: str) -> str:
    """Build a portable, lossless Markdown export."""
    parts = [f"# {structured['title']}"]
    if structured.get("summary"):
        parts.extend(["", structured["summary"]])
    parts.extend(["", structured["structured_text"]])

    sections = (
        ("Решения", structured.get("decisions", [])),
        (
            "Следующие действия",
            [
                " — ".join(
                    part
                    for part in (
                        action.get("task", ""),
                        f"ответственный: {action['owner']}"
                        if action.get("owner")
                        else "",
                        f"срок: {action['deadline']}"
                        if action.get("deadline")
                        else "",
                    )
                    if part
                )
                for action in structured.get("actions", [])
            ],
        ),
        ("Идеи и гипотезы", structured.get("ideas", [])),
        ("Открытые вопросы", structured.get("open_questions", [])),
    )
    for heading, items in sections:
        if items:
            parts.extend(["", f"## {heading}", *[f"- {item}" for item in items]])

    if structured.get("tags"):
        parts.extend(["", "## Теги", " ".join(f"`{tag}`" for tag in structured["tags"])])
    parts.extend(["", "---", "", "## Оригинальная запись", "", source_text.strip(), ""])
    return "\n".join(parts)


# Compatibility wrapper for callers from older versions of this repository.
async def ask_grok(user_message: str, history: list | None = None) -> str:
    del history
    structured = await structure_thoughts(user_message)
    return structured["structured_text"]
