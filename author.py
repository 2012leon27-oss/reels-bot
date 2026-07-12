"""Classify who sent a Telegram Business message."""

from __future__ import annotations

from typing import Literal

from aiogram.types import Message

MessageAuthor = Literal[
    "contact",
    "owner",
    "ai_assistant",
    "other_bot",
    "automatic",
]


def classify_message_author(
    message: Message,
    bot_id: int,
    owner_telegram_id: int,
) -> MessageAuthor:
    """
    Determine message author for persistence and policy.

    Rules:
    - sender_business_bot.id == bot_id → ai_assistant (our bot's Business reply)
    - other business bot sender → other_bot
    - from.id == owner_telegram_id → owner
    - is_from_offline → automatic (Telegram offline/auto-reply)
    - else → contact
    """
    sender_bot = message.sender_business_bot
    if sender_bot is not None:
        if int(sender_bot.id) == int(bot_id):
            return "ai_assistant"
        return "other_bot"

    if message.from_user is not None and int(message.from_user.id) == int(
        owner_telegram_id
    ):
        return "owner"

    if bool(getattr(message, "is_from_offline", False)):
        return "automatic"

    return "contact"
