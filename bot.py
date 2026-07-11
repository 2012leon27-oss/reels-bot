"""Telegram Business assistant entrypoint and update handlers."""

from __future__ import annotations

import asyncio
import html
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import ConfigurationError, Settings
from context_loader import ContextLoader
from database import Database
from decision import ReplyDecision
from keep_alive import start_webserver
from llm_client import LLMClient, LLMServiceError
from policy import escalation_reason

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def message_text(message: Message) -> tuple[str, bool]:
    if message.text:
        return message.text, True
    if message.caption:
        return f"[{message.content_type}] {message.caption}", False
    return f"[{message.content_type}]", False


def stored_contact_name(contact: dict[str, Any]) -> str:
    first = contact.get("first_name") or ""
    last = contact.get("last_name") or ""
    name = " ".join(part for part in (first, last) if part).strip()
    return name or contact.get("username") or str(contact["chat_id"])


def review_keyboard(pending_id: int, has_draft: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_draft:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Отправить черновик",
                    callback_data=f"send:{pending_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="👤 Отвечу сам", callback_data=f"mine:{pending_id}"
            ),
            InlineKeyboardButton(
                text="🚫 Не отвечать", callback_data=f"ignore:{pending_id}"
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


class Assistant:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot = Bot(settings.bot_token)
        self.db = Database(settings.database_url)
        self.llm = LLMClient(settings)
        self.contexts = ContextLoader(Path(__file__).resolve().parent / "contexts")
        self.router = Router(name="business_assistant")
        self._inbox_wakeup = asyncio.Event()
        self._notification_lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._register_handlers()

    def _is_owner(self, user_id: int | None) -> bool:
        return user_id == self.settings.owner_id

    async def _save_connection(self, connection: BusinessConnection) -> bool:
        if connection.user.id != self.settings.owner_id:
            logger.warning(
                "Rejected business connection %s from unexpected owner %s",
                connection.id,
                connection.user.id,
            )
            return False
        rights = connection.rights
        await self.db.upsert_connection(
            connection_id=connection.id,
            owner_user_id=connection.user.id,
            owner_chat_id=connection.user_chat_id,
            is_enabled=connection.is_enabled,
            can_reply=bool(rights and rights.can_reply),
            can_read_messages=bool(rights and rights.can_read_messages),
        )
        return True

    async def _ensure_connection(self, connection_id: str) -> dict[str, Any] | None:
        stored = await self.db.get_connection(connection_id)
        if stored:
            return stored
        try:
            remote = await self.bot.get_business_connection(
                business_connection_id=connection_id
            )
        except Exception:
            logger.exception("Could not fetch business connection %s", connection_id)
            return None
        if not await self._save_connection(remote):
            return None
        return await self.db.get_connection(connection_id)

    async def _notify_owner(
        self, pending: dict[str, Any], *, force: bool = False
    ) -> None:
        async with self._notification_lock:
            current = await self.db.get_pending(
                int(pending["id"]), self.settings.owner_id
            )
            if not current or current["status"] != "pending":
                return
            if current.get("notification_message_id") and not force:
                return
            pending = current
            incoming = html.escape(str(pending["incoming_text"])[:1700])
            draft = pending.get("suggested_reply")
            draft_block = (
                f"\n\n<b>Черновик:</b>\n{html.escape(str(draft)[:1100])}"
                if draft
                else ""
            )
            text = (
                "🔔 <b>Нужно ваше решение</b>\n\n"
                f"<b>Собеседник:</b> {html.escape(str(pending['contact_name'])[:200])}\n"
                f"<b>chat_id:</b> <code>{pending['chat_id']}</code>\n"
                f"<b>Причина:</b> {html.escape(str(pending['reason'])[:500])}\n\n"
                f"<b>Сообщение:</b>\n{incoming}"
                f"{draft_block}\n\n"
                "Можно нажать кнопку или ответить текстом прямо на это уведомление."
            )
            notification = await self.bot.send_message(
                self.settings.owner_id,
                text,
                parse_mode="HTML",
                reply_markup=review_keyboard(int(pending["id"]), bool(draft)),
            )
            await self.db.set_pending_notification(
                int(pending["id"]), notification.message_id
            )

    async def _escalate(
        self,
        *,
        connection_id: str,
        chat_id: int,
        incoming_message_id: int,
        contact: dict[str, Any],
        incoming_text: str,
        reason: str,
        draft: str | None,
    ) -> None:
        result = await self.db.create_pending(
            connection_id=connection_id,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            contact_name=stored_contact_name(contact),
            incoming_text=incoming_text,
            suggested_reply=draft,
            reason=reason,
        )
        if result is None:
            logger.info(
                "Skipped stale escalation for %s/%s",
                chat_id,
                incoming_message_id,
            )
            return
        pending, _created = result
        if pending.get("notification_message_id"):
            return
        await self._notify_owner(pending)

    async def _send_business_reply(
        self,
        *,
        connection_id: str,
        chat_id: int,
        text: str,
        prevalidated: bool = False,
    ) -> Message:
        if not prevalidated:
            connection = await self._ensure_connection(connection_id)
            if not connection or not connection["is_enabled"]:
                raise RuntimeError("Business connection is unavailable")
            if connection["owner_user_id"] != self.settings.owner_id:
                raise RuntimeError("Business connection belongs to a different owner")
            if not connection["can_reply"]:
                raise RuntimeError("Bot has no right to reply through this connection")

        sent = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            business_connection_id=connection_id,
            parse_mode=None,
        )
        try:
            await self.db.insert_message(
                connection_id=connection_id,
                chat_id=chat_id,
                telegram_message_id=sent.message_id,
                role="owner",
                content=text,
            )
        except Exception:
            # Telegram already accepted the message. Never retry delivery merely
            # because local bookkeeping failed; the outgoing update can reconcile it.
            logger.exception("Reply sent, but outgoing history could not be saved")
        return sent

    async def _process_incoming(self, incoming: dict[str, Any]) -> None:
        connection_id = str(incoming["connection_id"])
        chat_id = int(incoming["chat_id"])
        incoming_message_id = int(incoming["telegram_message_id"])
        connection = await self._ensure_connection(connection_id)
        if not connection or connection["owner_user_id"] != self.settings.owner_id:
            return

        incoming_text = str(incoming["content"])
        is_text = bool(incoming["is_text"])
        context = self.contexts.load(chat_id)
        paused = await self.db.is_paused(self.settings.owner_id)
        policy_reason = escalation_reason(
            text=incoming_text if is_text else None,
            is_text_message=is_text,
            runtime_paused=paused,
            auto_reply_enabled=self.settings.auto_reply_enabled,
            context_configured=context.is_configured,
            contact_trusted=bool(incoming["trusted_for_auto_reply"]),
            escalate_unknown_contacts=self.settings.escalate_unknown_contacts,
        )

        decision: ReplyDecision | None = None
        can_ask_llm = (
            is_text
            and context.is_configured
            and self.settings.auto_reply_enabled
            and not paused
        )
        if can_ask_llm:
            history = await self.db.get_history(
                connection_id=connection_id,
                chat_id=chat_id,
                limit=self.settings.history_limit,
                exclude_message_id=incoming_message_id,
            )
            try:
                decision = await self.llm.decide(
                    incoming_text=incoming_text,
                    contact_name=stored_contact_name(incoming),
                    contact_notes=str(incoming["notes"]),
                    context=context,
                    history=history,
                )
            except LLMServiceError:
                logger.exception("LLM failed for chat %s", chat_id)
                policy_reason = policy_reason or "ошибка языковой модели"

        should_escalate = (
            decision is None
            or decision.must_escalate(
                self.settings.confidence_threshold, policy_reason
            )
            or not connection["is_enabled"]
            or not connection["can_reply"]
        )
        if should_escalate:
            reasons = [
                reason
                for reason in (
                    policy_reason,
                    decision.reason if decision and decision.action == "escalate" else None,
                    (
                        f"низкая уверенность: {decision.confidence:.2f}"
                        if decision
                        and decision.confidence < self.settings.confidence_threshold
                        else None
                    ),
                    "нет права Telegram на ответы"
                    if not connection["can_reply"]
                    else None,
                    "Business-подключение отключено"
                    if not connection["is_enabled"]
                    else None,
                )
                if reason
            ]
            await self._escalate(
                connection_id=connection_id,
                chat_id=chat_id,
                incoming_message_id=incoming_message_id,
                contact=incoming,
                incoming_text=incoming_text,
                reason="; ".join(dict.fromkeys(reasons)) or "требуется решение владельца",
                draft=decision.reply if decision else None,
            )
            await self.db.mark_message_processed(
                connection_id, chat_id, incoming_message_id
            )
            return

        assert decision is not None and decision.reply is not None
        state_is_current = await self.db.reserve_auto_reply(
            owner_id=self.settings.owner_id,
            connection_id=connection_id,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            require_trusted=self.settings.escalate_unknown_contacts,
        )
        if not state_is_current:
            await self._escalate(
                connection_id=connection_id,
                chat_id=chat_id,
                incoming_message_id=incoming_message_id,
                contact=incoming,
                incoming_text=incoming_text,
                reason=(
                    "состояние чата изменилось во время подготовки ответа "
                    "(пауза, блокировка, ручной ответ, редактирование или удаление)"
                ),
                draft=decision.reply,
            )
            await self.db.mark_message_processed(
                connection_id, chat_id, incoming_message_id
            )
            return

        try:
            await self._send_business_reply(
                connection_id=connection_id,
                chat_id=chat_id,
                text=decision.reply,
                prevalidated=True,
            )
        except Exception:
            logger.exception("Automatic business reply could not be delivered")
            await self._escalate(
                connection_id=connection_id,
                chat_id=chat_id,
                incoming_message_id=incoming_message_id,
                contact=incoming,
                incoming_text=incoming_text,
                reason=(
                    "статус доставки автоматического ответа неизвестен; "
                    "проверьте чат перед ручным ответом"
                ),
                draft=None,
            )
        await self.db.mark_message_processed(
            connection_id, chat_id, incoming_message_id
        )

    async def _send_pending_text(
        self, pending_id: int, text: str
    ) -> tuple[bool, str]:
        pending = await self.db.claim_pending(pending_id, self.settings.owner_id)
        if not pending:
            return False, "Это обращение уже обработано."
        try:
            await self._send_business_reply(
                connection_id=str(pending["connection_id"]),
                chat_id=int(pending["chat_id"]),
                text=text,
                prevalidated=True,
            )
        except (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramNotFound,
            TelegramRetryAfter,
        ) as exc:
            await self.db.release_pending(pending_id)
            logger.exception("Owner-approved reply failed")
            return False, f"Не удалось отправить: {exc}"
        except Exception:
            logger.exception("Owner-approved reply has unknown delivery status")
            await self.db.resolve_pending(
                pending_id, "send_unknown", self.settings.owner_id
            )
            return (
                False,
                "Статус доставки неизвестен. Проверьте чат перед повторным ответом.",
            )
        await self.db.resolve_pending(pending_id, "sent", self.settings.owner_id)
        return True, "Ответ отправлен."

    def _register_handlers(self) -> None:
        router = self.router

        @router.business_connection()
        async def on_business_connection(connection: BusinessConnection) -> None:
            accepted = await self._save_connection(connection)
            if not accepted:
                return
            state = "подключён" if connection.is_enabled else "отключён"
            can_reply = bool(connection.rights and connection.rights.can_reply)
            await self.bot.send_message(
                self.settings.owner_id,
                f"Business-бот {state}. Право отвечать: "
                f"{'есть' if can_reply else 'нет'}.",
            )

        @router.business_message()
        async def on_business_message(message: Message) -> None:
            if not message.business_connection_id:
                return
            is_outgoing = bool(
                message.sender_business_bot
                or (message.from_user and self._is_owner(message.from_user.id))
            )
            if is_outgoing:
                text, _ = message_text(message)
                connection = await self._ensure_connection(
                    message.business_connection_id
                )
                if not connection:
                    return
                await self.db.insert_message(
                    connection_id=message.business_connection_id,
                    chat_id=message.chat.id,
                    telegram_message_id=message.message_id,
                    role="owner",
                    content=text,
                )
                return
            connection = await self._ensure_connection(message.business_connection_id)
            if not connection or connection["owner_user_id"] != self.settings.owner_id:
                return
            incoming_text, is_text = message_text(message)
            await self.db.upsert_contact(
                connection_id=message.business_connection_id,
                chat_id=message.chat.id,
                telegram_user_id=message.from_user.id if message.from_user else None,
                username=message.chat.username,
                first_name=message.chat.first_name,
                last_name=message.chat.last_name,
            )
            needs_processing = await self.db.begin_incoming_message(
                connection_id=message.business_connection_id,
                chat_id=message.chat.id,
                telegram_message_id=message.message_id,
                content=incoming_text,
                is_text=is_text,
            )
            if needs_processing:
                self._inbox_wakeup.set()

        @router.edited_business_message()
        async def on_edited_business_message(message: Message) -> None:
            if not message.business_connection_id:
                return
            connection = await self._ensure_connection(message.business_connection_id)
            if not connection or connection["owner_user_id"] != self.settings.owner_id:
                return
            text, is_text = message_text(message)
            is_outgoing = bool(
                message.sender_business_bot
                or (message.from_user and self._is_owner(message.from_user.id))
            )
            if is_outgoing:
                await self.db.insert_message(
                    connection_id=message.business_connection_id,
                    chat_id=message.chat.id,
                    telegram_message_id=message.message_id,
                    role="owner",
                    content=text,
                )
            else:
                await self.db.upsert_contact(
                    connection_id=message.business_connection_id,
                    chat_id=message.chat.id,
                    telegram_user_id=message.from_user.id if message.from_user else None,
                    username=message.chat.username,
                    first_name=message.chat.first_name,
                    last_name=message.chat.last_name,
                )
                await self.db.begin_incoming_message(
                    connection_id=message.business_connection_id,
                    chat_id=message.chat.id,
                    telegram_message_id=message.message_id,
                    content=text,
                    is_text=is_text,
                )
            await self.db.edit_message(
                message.business_connection_id,
                message.chat.id,
                message.message_id,
                text,
            )
            self._inbox_wakeup.set()

        @router.deleted_business_messages()
        async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
            connection = await self._ensure_connection(event.business_connection_id)
            if not connection or connection["owner_user_id"] != self.settings.owner_id:
                return
            await self.db.mark_messages_deleted(
                event.business_connection_id,
                event.chat.id,
                event.message_ids,
            )

        @router.message(CommandStart())
        async def owner_start(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                return
            await message.answer(
                "Личный Business-ассистент запущен.\n\n"
                "Сначала подключите этого бота в Telegram → Настройки → "
                "Telegram Business → Чат-боты и дайте право отвечать.\n\n"
                "Команды: /status, /pause, /resume, /pending, /allow, /block, /note"
            )

        @router.message(Command("status"))
        async def owner_status(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                return
            paused = await self.db.is_paused(self.settings.owner_id)
            context_ready = self.contexts.load(0).is_configured
            stats = await self.db.stats(self.settings.owner_id)
            await message.answer(
                "Статус ассистента:\n"
                f"• системный автоответ: {'включён' if self.settings.auto_reply_enabled else 'выключен'}\n"
                f"• ручная пауза: {'да' if paused else 'нет'}\n"
                f"• контекст заполнен: {'да' if context_ready else 'нет'}\n"
                f"• контактов: {stats['contacts']}\n"
                f"• сообщений в истории: {stats['messages']}\n"
                f"• ждут решения: {stats['pending']}"
            )

        @router.message(Command("pause"))
        async def owner_pause(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                return
            await self.db.set_paused(self.settings.owner_id, True)
            await message.answer("Автоответы приостановлены. Все новые сообщения идут вам.")

        @router.message(Command("resume"))
        async def owner_resume(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                return
            if not self.settings.auto_reply_enabled:
                await message.answer(
                    "AUTO_REPLY_ENABLED=false. Сначала включите переменную окружения "
                    "и перезапустите сервис."
                )
                return
            if not self.contexts.load(0).is_configured:
                await message.answer(
                    "Контекст не заполнен. Заполните contexts/owner.md, style.md и rules.md."
                )
                return
            await self.db.set_paused(self.settings.owner_id, False)
            await message.answer("Автоответы включены для разрешённых контактов.")

        @router.message(Command("pending"))
        async def owner_pending(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                return
            items = await self.db.list_pending(self.settings.owner_id)
            if not items:
                await message.answer("Необработанных сообщений нет.")
                return
            for item in items:
                try:
                    await self._notify_owner(item, force=True)
                except Exception:
                    logger.exception("Could not resend pending review %s", item["id"])

        @router.message(Command("allow", "block", "note"))
        async def owner_contact_command(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                return
            parts = (message.text or "").split(maxsplit=2)
            command = parts[0].split("@", 1)[0].lstrip("/")
            if len(parts) < 2:
                await message.answer(f"Формат: /{command} <chat_id>" + (
                    " <заметка>" if command == "note" else ""
                ))
                return
            try:
                chat_id = int(parts[1])
            except ValueError:
                await message.answer("chat_id должен быть числом из уведомления.")
                return
            contact = await self.db.find_contact(self.settings.owner_id, chat_id)
            if not contact:
                await message.answer("Контакт с таким chat_id не найден.")
                return
            if command == "note":
                if len(parts) < 3 or not parts[2].strip():
                    await message.answer("Формат: /note <chat_id> <заметка>")
                    return
                await self.db.set_contact_notes(
                    str(contact["connection_id"]), chat_id, parts[2].strip()
                )
                await message.answer("Заметка сохранена.")
            else:
                trusted = command == "allow"
                await self.db.set_contact_trusted(
                    str(contact["connection_id"]), chat_id, trusted
                )
                await message.answer(
                    "Автоответы для контакта разрешены."
                    if trusted
                    else "Автоответы для контакта запрещены."
                )

        @router.callback_query()
        async def owner_callback(callback: CallbackQuery) -> None:
            if not self._is_owner(callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            data = callback.data or ""
            try:
                action, raw_id = data.split(":", 1)
                pending_id = int(raw_id)
            except (ValueError, TypeError):
                await callback.answer("Неизвестная кнопка.", show_alert=True)
                return
            pending = await self.db.get_pending(pending_id, self.settings.owner_id)
            if not pending:
                await callback.answer("Обращение не найдено.", show_alert=True)
                return

            if action == "send":
                draft = pending.get("suggested_reply")
                if not draft:
                    await callback.answer("У этого обращения нет черновика.", show_alert=True)
                    return
                success, result = await self._send_pending_text(pending_id, str(draft))
            elif action in {"ignore", "mine"}:
                status = "ignored" if action == "ignore" else "owner_handled"
                success = await self.db.resolve_pending(
                    pending_id, status, self.settings.owner_id
                )
                result = "Отмечено." if success else "Уже обработано."
            else:
                await callback.answer("Неизвестная кнопка.", show_alert=True)
                return

            await callback.answer(result, show_alert=not success)
            if success and callback.message:
                with suppress(Exception):
                    await callback.message.edit_reply_markup(reply_markup=None)

        @router.message()
        async def owner_custom_reply(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                return
            if (
                message.chat.id != self.settings.owner_id
                or not message.text
                or not message.reply_to_message
            ):
                return
            pending = await self.db.get_pending_by_notification(
                message.reply_to_message.message_id,
                self.settings.owner_id,
            )
            if not pending:
                return
            success, result = await self._send_pending_text(
                int(pending["id"]), message.text
            )
            await message.answer(result)
            if success:
                with suppress(Exception):
                    await self.bot.edit_message_reply_markup(
                        chat_id=self.settings.owner_id,
                        message_id=message.reply_to_message.message_id,
                        reply_markup=None,
                    )

    async def _retry_notifications(self) -> None:
        while not self._stopping.is_set():
            try:
                items = await self.db.list_pending(
                    self.settings.owner_id,
                    limit=20,
                    only_unnotified=True,
                )
                for item in items:
                    try:
                        await self._notify_owner(item)
                    except Exception:
                        logger.exception(
                            "Notification retry failed for pending %s", item["id"]
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pending notification worker failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=30)
            except TimeoutError:
                pass

    async def _process_inbox(self) -> None:
        while not self._stopping.is_set():
            try:
                items = await self.db.list_unprocessed_incoming(
                    self.settings.owner_id,
                    limit=20,
                )
                if not items:
                    self._inbox_wakeup.clear()
                    try:
                        await asyncio.wait_for(self._inbox_wakeup.wait(), timeout=10)
                    except TimeoutError:
                        pass
                    continue
                for item in items:
                    if self._stopping.is_set():
                        break
                    try:
                        await self._process_incoming(item)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Inbox processing failed for %s/%s; will retry",
                            item["chat_id"],
                            item["telegram_message_id"],
                        )
                if not self._stopping.is_set():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Inbox worker failed")
                await asyncio.sleep(5)

    async def run(self) -> None:
        web_runner = None
        notification_task: asyncio.Task[None] | None = None
        inbox_task: asyncio.Task[None] | None = None
        try:
            await self.db.connect(
                owner_id=self.settings.owner_id,
                initially_paused=not self.settings.auto_reply_enabled,
            )
            recovered = await self.db.recover_stale_pending(self.settings.owner_id)
            if recovered:
                logger.warning("Recovered %s stale pending reviews", recovered)
            web_runner = await start_webserver(self.settings.port, self.db.ping)
            notification_task = asyncio.create_task(
                self._retry_notifications(),
                name="pending-notification-retry",
            )
            inbox_task = asyncio.create_task(
                self._process_inbox(),
                name="durable-inbox-worker",
            )
            dispatcher = Dispatcher()
            dispatcher.include_router(self.router)
            await self.bot.delete_webhook(drop_pending_updates=False)
            logger.info("Telegram Business assistant started")
            await dispatcher.start_polling(
                self.bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                handle_as_tasks=False,
                close_bot_session=False,
            )
        finally:
            self._stopping.set()
            self._inbox_wakeup.set()
            workers = [
                task for task in (inbox_task, notification_task) if task is not None
            ]
            if workers:
                done, pending = await asyncio.wait(workers, timeout=40)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)
            if web_runner:
                with suppress(Exception):
                    await web_runner.cleanup()
            with suppress(Exception):
                await self.llm.close()
            with suppress(Exception):
                await self.db.close()
            with suppress(Exception):
                await self.bot.session.close()


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        logger.critical("Configuration error: %s", exc)
        raise SystemExit(2) from exc
    await Assistant(settings).run()


if __name__ == "__main__":
    asyncio.run(main())
