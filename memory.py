"""Session memory, summarization helpers, and Syntx chat rotation."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

from store import Store

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    return float(raw)


MEMORY_RETENTION_DAYS = _env_int("MEMORY_RETENTION_DAYS", 15)
RECENT_MESSAGE_LIMIT = _env_int("RECENT_MESSAGE_LIMIT", 30)
SESSION_INACTIVITY_MINUTES = _env_int("SESSION_INACTIVITY_MINUTES", 30)
SYNTX_CHAT_MAX_MESSAGES = _env_int("SYNTX_CHAT_MAX_MESSAGES", 40)
SYNTX_CHAT_ROTATION_HOURS = _env_float("SYNTX_CHAT_ROTATION_HOURS", 24.0)

STYLE_SUMMARY_MIN_OWNER_MESSAGES = 10


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _conversation_last_activity(conversation: dict[str, Any]) -> datetime | None:
    for key in ("updated_at", "created_at"):
        parsed = _parse_timestamp(conversation.get(key))
        if parsed is not None:
            return parsed
    return None


def session_inactive(conversation: dict[str, Any]) -> bool:
    last = _conversation_last_activity(conversation)
    if last is None:
        return False
    threshold = datetime.now(timezone.utc) - timedelta(
        minutes=SESSION_INACTIVITY_MINUTES
    )
    return last < threshold


def build_summary_prompt_data(
    *,
    topic: str = "",
    goals: list[str] | None = None,
    facts: list[str] | None = None,
    agreements: list[str] | None = None,
    promises: list[str] | None = None,
    open_questions: list[str] | None = None,
    next_action: str = "",
) -> dict[str, Any]:
    """Structured payload for LLM session summarization."""
    return {
        "topic": topic,
        "goals": goals or [],
        "facts": facts or [],
        "agreements": agreements or [],
        "promises": promises or [],
        "open_questions": open_questions or [],
        "next_action": next_action,
    }


def summary_prompt_data_to_text(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def should_rotate_syntx_chat(conversation: dict[str, Any]) -> bool:
    """True when message count or age exceeds configured limits."""
    if not conversation.get("syntx_chat_url"):
        return False

    message_count = int(conversation.get("syntx_message_count") or 0)
    if message_count >= SYNTX_CHAT_MAX_MESSAGES:
        return True

    created_at = _parse_timestamp(conversation.get("syntx_chat_created_at"))
    if created_at is None:
        return False

    age = datetime.now(timezone.utc) - created_at
    return age >= timedelta(hours=SYNTX_CHAT_ROTATION_HOURS)


async def maybe_close_session_and_summarize(
    store: Store,
    conversation: dict[str, Any],
    *,
    summary_text: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> dict[str, Any] | None:
    """
    After inactivity, persist a memory summary and optionally rotate Syntx chat.
    Caller supplies summary_text (e.g. from LLM); if omitted, only rotation check runs.
    """
    if not session_inactive(conversation):
        return None

    conversation_id = int(conversation["id"])
    result: dict[str, Any] = {"conversation_id": conversation_id, "actions": []}

    if summary_text:
        now = datetime.now(timezone.utc)
        start = period_start or _parse_timestamp(conversation.get("created_at")) or now
        end = period_end or now
        summary = await store.save_memory_summary(
            conversation_id=conversation_id,
            period_start=start,
            period_end=end,
            summary=summary_text,
        )
        result["summary"] = summary
        result["actions"].append("saved_summary")

    if should_rotate_syntx_chat(conversation):
        rotated = await store.clear_syntx_chat(conversation_id)
        result["rotated_conversation"] = rotated
        result["actions"].append("rotated_syntx_chat")

    return result


def style_summary_from_owner_messages(
    messages: list[dict[str, Any]],
    *,
    min_messages: int = STYLE_SUMMARY_MIN_OWNER_MESSAGES,
) -> str | None:
    """
    Build a plain-text style digest from owner-authored messages only.
    Returns None when fewer than min_messages owner lines are available.
    """
    owner_lines = [
        (message.get("text") or "").strip()
        for message in messages
        if message.get("author_type") == "owner"
        and (message.get("text") or "").strip()
        and message.get("deleted_at") is None
    ]
    if len(owner_lines) < min_messages:
        return None

    sample = owner_lines[-min_messages:]
    header = (
        f"Стиль владельца (по {len(sample)} сообщениям):\n"
        "- короткие фразы, без лишней формальности\n"
        "- ниже примеры реальных реплик\n\n"
    )
    body = "\n".join(f"• {line}" for line in sample)
    return header + body
