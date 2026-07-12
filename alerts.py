"""Owner alerts for escalations: Telegram, optional SMS stub, critical repeats."""

from __future__ import annotations

import asyncio
import html
import logging
import os
from typing import Any, Literal

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

Urgency = Literal["normal", "urgent", "critical"]

_URGENCY_MARKERS: dict[Urgency, str] = {
    "normal": "🔔",
    "urgent": "⚠️ <b>СРОЧНО</b>",
    "critical": "🚨 <b>КРИТИЧНО</b>",
}


def _contact_link(contact: dict[str, Any]) -> str:
    username = (contact.get("username") or "").strip().lstrip("@")
    if username:
        return f"@{html.escape(username)}"
    user_id = contact.get("telegram_user_id") or contact.get("user_id")
    if user_id:
        return f'<a href="tg://user?id={int(user_id)}">контакт</a>'
    name = contact.get("first_name") or contact.get("name") or "контакт"
    return html.escape(str(name)[:200])


def alert_keyboard(alert_id: str, conversation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принял",
                    callback_data=f"ack:{alert_id}",
                ),
                InlineKeyboardButton(
                    text="🤖 Вернуть ИИ",
                    callback_data=f"resume_ai:{conversation_id}",
                ),
            ]
        ]
    )


def _format_recent_lines(recent_lines: list[str] | None, limit: int = 8) -> str:
    if not recent_lines:
        return ""
    lines = [html.escape(line[:400]) for line in recent_lines[-limit:]]
    body = "\n".join(f"• {line}" for line in lines)
    return f"\n\n<b>Последние реплики:</b>\n{body}"


async def send_owner_alert(
    bot: Bot,
    owner_id: int,
    contact: dict[str, Any],
    reason: str,
    urgency: Urgency,
    recent_lines: list[str] | None,
    conversation_id: str,
    alert_id: str,
) -> Any:
    """Send a Telegram alert to the owner with ack / resume_ai buttons."""
    marker = _URGENCY_MARKERS.get(urgency, _URGENCY_MARKERS["normal"])
    contact_ref = _contact_link(contact)
    display_name = html.escape(
        str(contact.get("first_name") or contact.get("name") or "Собеседник")[:200]
    )
    text = (
        f"{marker}\n\n"
        f"<b>Собеседник:</b> {display_name} ({contact_ref})\n"
        f"<b>Диалог:</b> <code>{html.escape(conversation_id)}</code>\n"
        f"<b>Причина:</b> {html.escape(reason[:800])}"
        f"{_format_recent_lines(recent_lines)}"
    )
    return await bot.send_message(
        owner_id,
        text,
        parse_mode="HTML",
        disable_notification=False,
        reply_markup=alert_keyboard(alert_id, conversation_id),
    )


async def send_sms_alert(
    *,
    phone: str,
    message: str,
    urgency: Urgency,
) -> bool:
    """
    Optional SMS stub. Returns True only when a provider is configured and send
    is attempted. Never claims success without SMS_PROVIDER and SMS_API_KEY.
    """
    provider = (os.getenv("SMS_PROVIDER") or "").strip()
    api_key = (os.getenv("SMS_API_KEY") or "").strip()
    if not provider or not api_key:
        logger.info(
            "SMS not configured (SMS_PROVIDER/SMS_API_KEY empty); Telegram remains fallback"
        )
        return False

    from_number = (os.getenv("SMS_FROM") or "").strip()
    prefix = "КРИТИЧНО: " if urgency == "critical" else (
        "СРОЧНО: " if urgency == "urgent" else ""
    )
    payload = f"{prefix}{message}"[:320]

    # Stub: log intent; integrate Twilio/etc. when provider keys are set.
    logger.warning(
        "SMS stub: would send via %r from %r to %r: %s",
        provider,
        from_number or "(default)",
        phone,
        payload[:120],
    )
    return False


class CriticalAlertRepeater:
    """Per-alert repeater: up to 3 repeats every 120s until acknowledged."""

    def __init__(
        self,
        *,
        bot: Bot,
        owner_id: int,
        contact: dict[str, Any],
        reason: str,
        recent_lines: list[str] | None,
        conversation_id: str,
        alert_id: str,
        max_repeats: int = 3,
        interval_seconds: float = 120.0,
        on_repeat: Any | None = None,
    ) -> None:
        self._bot = bot
        self._owner_id = owner_id
        self._contact = contact
        self._reason = reason
        self._recent_lines = recent_lines
        self._conversation_id = conversation_id
        self._alert_id = alert_id
        self._max_repeats = max_repeats
        self._interval = interval_seconds
        self._on_repeat = on_repeat
        self._acknowledged = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._repeats_sent = 0

    @property
    def alert_id(self) -> str:
        return self._alert_id

    @property
    def repeats_sent(self) -> int:
        return self._repeats_sent

    def acknowledge(self) -> None:
        self._acknowledged.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"alert-repeat:{self._alert_id}"
            )

    def stop(self) -> None:
        self.acknowledge()
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        try:
            while self._repeats_sent < self._max_repeats:
                try:
                    await asyncio.wait_for(
                        self._acknowledged.wait(),
                        timeout=self._interval,
                    )
                    return
                except TimeoutError:
                    pass

                if self._acknowledged.is_set():
                    return

                self._repeats_sent += 1
                logger.info(
                    "Repeating critical alert %s (%s/%s)",
                    self._alert_id,
                    self._repeats_sent,
                    self._max_repeats,
                )
                if self._on_repeat is not None:
                    await self._on_repeat()
                await send_owner_alert(
                    self._bot,
                    self._owner_id,
                    self._contact,
                    f"{self._reason} (повтор {self._repeats_sent}/{self._max_repeats})",
                    "critical",
                    self._recent_lines,
                    self._conversation_id,
                    self._alert_id,
                )
        except asyncio.CancelledError:
            return


class CriticalAlertManager:
    """Tracks active critical repeaters; ack stops further spam."""

    def __init__(self) -> None:
        self._active: dict[str, CriticalAlertRepeater] = {}

    def schedule(
        self,
        *,
        bot: Bot,
        owner_id: int,
        contact: dict[str, Any],
        reason: str,
        recent_lines: list[str] | None,
        conversation_id: str,
        alert_id: str,
        on_repeat: Any | None = None,
    ) -> CriticalAlertRepeater:
        existing = self._active.get(alert_id)
        if existing:
            return existing
        repeater = CriticalAlertRepeater(
            bot=bot,
            owner_id=owner_id,
            contact=contact,
            reason=reason,
            recent_lines=recent_lines,
            conversation_id=conversation_id,
            alert_id=alert_id,
            on_repeat=on_repeat,
        )
        self._active[alert_id] = repeater
        repeater.start()
        return repeater

    def acknowledge(self, alert_id: str) -> None:
        repeater = self._active.pop(alert_id, None)
        if repeater:
            repeater.acknowledge()
            repeater.stop()

    def shutdown(self) -> None:
        for alert_id in list(self._active):
            self.acknowledge(alert_id)


def try_send_sms_alert(
    *,
    reason: str,
    urgency: Urgency,
    provider: str = "",
    api_key: str = "",
    sms_from: str = "",
    owner_phone: str = "",
) -> bool:
    """Send SMS only when provider keys exist; otherwise Telegram remains fallback."""
    if provider:
        os.environ.setdefault("SMS_PROVIDER", provider)
    if api_key:
        os.environ.setdefault("SMS_API_KEY", api_key)
    if sms_from:
        os.environ.setdefault("SMS_FROM", sms_from)
    if not owner_phone:
        logger.info("OWNER_PHONE empty; SMS skipped, Telegram remains fallback")
        return False
    # fire-and-forget sync stub
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            send_sms_alert(phone=owner_phone, message=reason, urgency=urgency)
        )
        return False
    except RuntimeError:
        return False
