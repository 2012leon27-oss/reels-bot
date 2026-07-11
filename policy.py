"""Deterministic safety checks that run before any automatic reply."""

from __future__ import annotations

import re


RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "деньги или оплата",
        re.compile(
            r"\b(деньг|оплат|цен[аы]|стоимост|перевод|сч[её]т|долг|"
            r"рубл|доллар|евро|крипт|банк|карт[ауы]|refund|payment|price)\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "договорённость или встреча",
        re.compile(
            r"\b(встреч|созвон|звонок|договор|контракт|подпис|срок|дедлайн|"
            r"адрес|приехать|записать|брон|appointment|meeting|contract)\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "конфликт или срочность",
        re.compile(
            r"\b(срочн|немедленно|жалоб|претензи|суд|полици|адвокат|"
            r"обман|мошенн|угроз|ненавиж|urgent|complaint|lawyer)\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "чувствительные данные",
        re.compile(
            r"\b(парол|код подтверждения|паспорт|инн|снилс|реквизит|документ|"
            r"справк|доверенност|cvv|password|passport|verification code|document)\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "здоровье или безопасность",
        re.compile(
            r"\b(врач|болезн|лекарств|диагноз|скорую|самоубий|умереть|"
            r"doctor|medicine|suicid|emergency)\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "личная или интимная тема",
        re.compile(
            r"\b(люблю|отношени|расстав|ревну|свидани|интим|секс|поцелу|"
            r"беремен|замуж|женить|romantic|relationship|intimate|sex)\w*",
            re.IGNORECASE,
        ),
    ),
)


def escalation_reason(
    *,
    text: str | None,
    is_text_message: bool,
    runtime_paused: bool,
    auto_reply_enabled: bool,
    context_configured: bool,
    contact_trusted: bool,
    escalate_unknown_contacts: bool,
) -> str | None:
    """Return a mandatory escalation reason, or None when LLM may decide."""
    if runtime_paused or not auto_reply_enabled:
        return "автоответы приостановлены"
    if not context_configured:
        return "контекст владельца ещё не заполнен"
    if not is_text_message or not text:
        return "сообщение содержит медиа или неподдерживаемый тип данных"
    if escalate_unknown_contacts and not contact_trusted:
        return "собеседник ещё не разрешён для автоответов"

    for label, pattern in RISK_PATTERNS:
        if pattern.search(text):
            return f"важная тема: {label}"
    return None
