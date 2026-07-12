"""Structured Claude output: reply | handoff | silent."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal


class InvalidAgentDecision(ValueError):
    """Raised when the model response cannot be used safely."""


Action = Literal["reply", "handoff", "silent"]
Urgency = Literal["normal", "urgent", "critical"]


@dataclass(frozen=True, slots=True)
class AgentDecision:
    action: Action
    urgency: Urgency
    reply: str
    handoff_reason: str
    confidence: float

    @classmethod
    def from_json(cls, raw: str) -> "AgentDecision":
        text = raw.strip()
        # Strip markdown fences if present.
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                text = "\n".join(lines[1:-1])
                if text.lstrip().lower().startswith("json"):
                    text = text.lstrip()[4:].lstrip()

        # Prefer first JSON object if prose surrounds it.
        if not text.startswith("{"):
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                text = match.group(0)

        try:
            payload: Any = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvalidAgentDecision("Model did not return valid JSON") from exc

        if not isinstance(payload, dict):
            raise InvalidAgentDecision("Model response must be a JSON object")

        action = payload.get("action")
        if action not in {"reply", "handoff", "silent"}:
            raise InvalidAgentDecision("action must be reply, handoff, or silent")

        urgency = payload.get("urgency", "normal")
        if urgency not in {"normal", "urgent", "critical"}:
            raise InvalidAgentDecision("urgency must be normal, urgent, or critical")

        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise InvalidAgentDecision("confidence must be a number")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise InvalidAgentDecision("confidence must be between 0 and 1")

        reply_value = payload.get("reply", "")
        if reply_value is None:
            reply_value = ""
        if not isinstance(reply_value, str):
            raise InvalidAgentDecision("reply must be a string")
        reply = reply_value.strip()
        if len(reply) > 4096:
            raise InvalidAgentDecision("reply exceeds Telegram's 4096 character limit")

        reason_value = payload.get("handoff_reason", "")
        if reason_value is None:
            reason_value = ""
        if not isinstance(reason_value, str):
            raise InvalidAgentDecision("handoff_reason must be a string")
        handoff_reason = reason_value.strip()[:500]

        if action == "reply" and not reply:
            raise InvalidAgentDecision("reply action requires non-empty reply")
        if action == "handoff" and not handoff_reason:
            handoff_reason = "модель запросила передачу владельцу"
        if action == "silent":
            reply = ""

        return cls(
            action=action,
            urgency=urgency,
            reply=reply,
            handoff_reason=handoff_reason,
            confidence=confidence,
        )

    def must_handoff(self, threshold: float, policy_reason: str | None) -> bool:
        if policy_reason:
            return True
        if self.action == "handoff":
            return True
        if self.confidence < threshold:
            return True
        if self.action == "reply" and not self.reply:
            return True
        return False

    @classmethod
    def handoff(cls, reason: str, urgency: Urgency = "urgent", reply: str = "") -> "AgentDecision":
        return cls(
            action="handoff",
            urgency=urgency,
            reply=reply,
            handoff_reason=reason,
            confidence=0.0,
        )

    @classmethod
    def silent(cls, reason: str = "") -> "AgentDecision":
        return cls(
            action="silent",
            urgency="normal",
            reply="",
            handoff_reason=reason,
            confidence=1.0,
        )
