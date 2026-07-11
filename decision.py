"""Validated decision returned by the language model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal


class InvalidDecision(ValueError):
    """Raised when the model response cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ReplyDecision:
    action: Literal["reply", "escalate"]
    confidence: float
    category: str
    reply: str | None
    reason: str

    @classmethod
    def from_json(cls, raw: str) -> "ReplyDecision":
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                text = "\n".join(lines[1:-1])
                if text.lstrip().startswith("json"):
                    text = text.lstrip()[4:].lstrip()

        try:
            payload: Any = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvalidDecision("Model did not return valid JSON") from exc

        if not isinstance(payload, dict):
            raise InvalidDecision("Model response must be a JSON object")

        action = payload.get("action")
        if action not in {"reply", "escalate"}:
            raise InvalidDecision("action must be reply or escalate")

        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise InvalidDecision("confidence must be a number")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise InvalidDecision("confidence must be between 0 and 1")

        category_value = payload.get("category")
        reason_value = payload.get("reason")
        if not isinstance(category_value, str):
            raise InvalidDecision("category must be a string")
        if not isinstance(reason_value, str):
            raise InvalidDecision("reason must be a string")
        category = category_value.strip()[:80] or "unknown"
        reason = reason_value.strip()[:500]
        reply_value = payload.get("reply")
        if reply_value is not None and not isinstance(reply_value, str):
            raise InvalidDecision("reply must be a string or null")
        reply = reply_value.strip() if reply_value is not None else None
        if reply == "":
            reply = None

        if action == "reply" and not reply:
            raise InvalidDecision("reply action requires non-empty reply")
        if reply and len(reply) > 4096:
            raise InvalidDecision("reply exceeds Telegram's 4096 character limit")
        if not reason:
            raise InvalidDecision("reason is required")

        return cls(
            action=action,
            confidence=confidence,
            category=category,
            reply=reply,
            reason=reason,
        )

    def must_escalate(self, threshold: float, policy_reason: str | None) -> bool:
        return bool(
            policy_reason
            or self.action == "escalate"
            or self.confidence < threshold
            or not self.reply
        )
