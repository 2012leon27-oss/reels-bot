"""Special communication policy for Андрей Усенко (urgent_only / restricted)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from agent.decision_v2 import AgentDecision

SAFE_ACK = (
    "Вижу, вопрос важный. Сейчас внимательно посмотрю и вернусь с ответом."
)

# Complex / project / commitment signals that must never get an AI decision reply.
URGENT_OR_COMPLEX = re.compile(
    r"(?i)"
    r"("
    r"срочн|критич|немедлен|asap|urgent|"
    r"дедлайн|срок|когда\s+будет|когда\s+сдад|"
    r"статус\s+проект|по\s+проект|прогресс|"
    r"доступ|логин|парол|vpn|сервер|репозитор|"
    r"деньг|оплат|сч[её]т|бюджет|цен[аы]|"
    r"договор|обязательств|гарант|"
    r"решен|утверд|согласу|"
    r"техническ|архитектур|интеграц|баг|ошибк\s+в\s+код|"
    r"файл|документ|настройк\s+telegram|бот\s+настрой"
    r")"
)

SIMPLE_GREETING = re.compile(
    r"(?i)^\s*(привет|здравствуй|добр(ый|ое|ого)\s+\w+|хай|hello|hi)[!.,\s]*$"
)


DecisionKind = Literal["silent", "handoff_ack"]


@dataclass(frozen=True, slots=True)
class AndreyPolicyResult:
    kind: DecisionKind
    decision: AgentDecision
    notify_owner: bool


def is_andrey_contact(profile: dict | None) -> bool:
    if not profile:
        return False
    if str(profile.get("ai_mode") or "").lower() == "urgent_only":
        alias = str(profile.get("owner_alias") or "").strip().lower()
        name = " ".join(
            part
            for part in (
                str(profile.get("first_name") or "").strip(),
                str(profile.get("last_name") or "").strip(),
            )
            if part
        ).lower()
        display = str(profile.get("display_name") or "").lower()
        haystack = f"{alias} {name} {display}"
        if "андрей" in haystack and "усенко" in haystack:
            return True
        # Bound contact with urgent_only and explicit relationship work still applies.
        if alias == "андрей усенко" or "андрей усенко" in display:
            return True
    return False


def evaluate_andrey_policy(message_text: str) -> AndreyPolicyResult:
    """Ordinary messages → silent. Urgent/complex → safe ack + handoff."""
    text = (message_text or "").strip()
    if not text:
        return AndreyPolicyResult(
            kind="silent",
            decision=AgentDecision.silent("пустое сообщение"),
            notify_owner=False,
        )

    if URGENT_OR_COMPLEX.search(text) or len(text) > 280:
        return AndreyPolicyResult(
            kind="handoff_ack",
            decision=AgentDecision(
                action="handoff",
                urgency="critical" if re.search(r"(?i)срочн|критич|asap", text) else "urgent",
                reply=SAFE_ACK,
                handoff_reason=(
                    "Андрей Усенко: срочный/сложный/проектный вопрос — "
                    "AI не принимает решений и не обещает сроки"
                ),
                confidence=0.0,
            ),
            notify_owner=True,
        )

    # Default for Андрей: do not answer for the owner.
    if SIMPLE_GREETING.match(text) or True:
        return AndreyPolicyResult(
            kind="silent",
            decision=AgentDecision.silent(
                "Андрей Усенко: обычное сообщение — AI молчит (urgent_only)"
            ),
            notify_owner=False,
        )
