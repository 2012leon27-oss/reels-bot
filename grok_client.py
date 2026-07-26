"""Cursor SDK client — official CURSOR_API_KEY + Grok 4.5.

Docs: https://cursor.com/docs/sdk/python
Keys: https://cursor.com/dashboard/api
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from config import settings
from prompts import STRUCTURING_SYSTEM_PROMPT

MODES = {
    "auto": "Автоматически выбери структуру, подходящую содержанию.",
    "project": "Оформи как проектный план: цель, контекст, этапы, риски и следующие действия.",
    "meeting": "Оформи как итоги встречи: контекст, обсуждение, решения, задачи и вопросы.",
    "content": "Оформи как контентный замысел: тезис, аудитория, смысловые блоки и план создания.",
    "personal": "Оформи как личную заметку: наблюдения, выводы, намерения и вопросы.",
}


class AIConfigurationError(RuntimeError):
    pass


class AIResponseError(RuntimeError):
    pass


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_structure(value: dict[str, Any]) -> dict[str, Any]:
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
    # Agent may wrap JSON with prose — extract first object.
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIResponseError("The model returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise AIResponseError("The model response must be a JSON object.")
    return normalize_structure(value)


def _extract_run_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    for attr in ("result", "text"):
        value = getattr(result, attr, None)
        if callable(value):
            try:
                value = value()
            except TypeError:
                pass
        if isinstance(value, str) and value.strip():
            return value
    return str(result)


def _call_cursor_agent(prompt: str) -> str:
    if not settings.ai_api_key:
        raise AIConfigurationError(
            "CURSOR_API_KEY missing. Put it in secrets/api.key "
            "(from https://cursor.com/dashboard/api)."
        )

    from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

    with tempfile.TemporaryDirectory(prefix="thought-architect-") as sandbox:
        # Empty sandbox: agent must not touch the real project.
        Path(sandbox, "README.txt").write_text(
            "Sandbox only. Do not edit files. Reply with JSON only.\n",
            encoding="utf-8",
        )
        options = AgentOptions(
            model=settings.ai_model or "grok-4.5",
            api_key=settings.ai_api_key,
            local=LocalAgentOptions(cwd=sandbox, setting_sources=[]),
        )
        run_result = Agent.prompt(prompt, options)
        text = _extract_run_text(run_result)
        if not text.strip():
            raise AIResponseError("Cursor agent returned an empty response.")
        return text


async def structure_thoughts(source_text: str, mode: str = "auto") -> dict[str, Any]:
    selected_mode = mode if mode in MODES else "auto"
    prompt = (
        f"{STRUCTURING_SYSTEM_PROMPT}\n\n"
        "CRITICAL RUNTIME RULES:\n"
        "- Do NOT use tools.\n"
        "- Do NOT read or edit files.\n"
        "- Do NOT create a plan document on disk.\n"
        "- Reply with ONE JSON object only. No markdown fences if possible.\n\n"
        f"Режим документа: {selected_mode}. {MODES[selected_mode]}\n\n"
        "Ниже исходная запись. Следуй критическим правилам и ничего не выдумывай.\n\n"
        f"<source>\n{source_text}\n</source>"
    )
    text = await asyncio.to_thread(_call_cursor_agent, prompt)
    return parse_structure_response(text)


async def transcribe_audio(
    audio: bytes | Any,
    *,
    filename: str = "recording.webm",
    content_type: str = "audio/webm",
    language: str | None = None,
) -> str:
    """Optional Whisper via Groq. Structuring uses Cursor SDK separately."""
    if not settings.transcription_api_key:
        raise AIConfigurationError(
            "Голос: либо вставь текст из Telegram, либо положи Whisper-ключ "
            "в secrets/transcription.key (Groq)."
        )
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.transcription_api_key,
        base_url=settings.transcription_base_url,
    )
    arguments: dict[str, Any] = {
        "file": (filename, audio, content_type),
        "model": settings.transcription_model,
        "response_format": "json",
        "temperature": 0,
    }
    if language and re.fullmatch(r"[a-z]{2}", language.lower()):
        arguments["language"] = language.lower()
    response = await client.audio.transcriptions.create(**arguments)
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        raise AIResponseError("Speech recognition returned an empty transcript.")
    return text


def render_markdown(structured: dict[str, Any], source_text: str) -> str:
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
        parts.extend(
            ["", "## Теги", " ".join(f"`{tag}`" for tag in structured["tags"])]
        )
    parts.extend(["", "---", "", "## Оригинальная запись", "", source_text.strip(), ""])
    return "\n".join(parts)


# Compatibility wrapper
async def ask_grok(user_message: str, history: list | None = None) -> str:
    del history
    structured = await structure_thoughts(user_message)
    return structured["structured_text"]
