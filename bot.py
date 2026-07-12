"""Telegram Business neuroagent — entrypoint and update handlers."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatAction
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
    Message,
    Update,
)

from agent.andrey_policy import evaluate_andrey_policy, is_andrey_contact
from agent.decision_v2 import AgentDecision, InvalidAgentDecision
from agent.prompt_builder import build_claude_prompt, format_dialogue_line, load_knowledge
from agent.queue import DialogueQueue, with_retries
from alerts import CriticalAlertManager, send_owner_alert, try_send_sms_alert
from author import classify_message_author
from config import ConfigurationError, Settings
from integrations.syntx import ModelMismatchError, SyntXBridgeError, SyntXClient
from keep_alive import start_webserver
from memory import (
    MEMORY_RETENTION_DAYS,
    RECENT_MESSAGE_LIMIT,
    maybe_close_session_and_summarize,
    should_rotate_syntx_chat,
    style_summary_from_owner_messages,
)
from policy import escalation_reason
from profiles import (
    apply_andrey_usenko_defaults,
    get_contact_communication_rules,
    get_contact_profile,
    get_memory_summaries,
    get_recent_dialogue,
    load_style_summary,
    normalize_person_name,
    save_style_summary,
    seed_pending_andrey_usenko_profile,
)
from store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
T = TypeVar("T")

SAFE_HANDOFF_ACK = (
    "Вижу, вопрос важный. Сейчас внимательно посмотрю и вернусь с ответом."
)


def message_text(message: Message) -> tuple[str, bool]:
    if message.text:
        return message.text, True
    if message.caption:
        return f"[{message.content_type}] {message.caption}", False
    return f"[{message.content_type}]", False


def contact_display_name(contact: dict[str, Any]) -> str:
    alias = (contact.get("owner_alias") or "").strip()
    if alias:
        return alias
    first = contact.get("first_name") or ""
    last = contact.get("last_name") or ""
    name = " ".join(part for part in (first, last) if part).strip()
    return name or contact.get("username") or str(contact.get("telegram_user_id") or "")


class Assistant:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot = Bot(settings.bot_token)
        self.store = Store(settings.database_url)
        self.syntx = SyntXClient(
            base_url=settings.syntx_bridge_url.rsplit("/", 1)[0],
            timeout=settings.syntx_timeout_seconds,
        )
        self.router = Router(name="business_neuroagent")
        self.queue = DialogueQueue()
        self.alert_manager = CriticalAlertManager()
        self._bot_id: int | None = None
        self._knowledge = load_knowledge()
        self._stopping = asyncio.Event()
        self._inbox_wakeup = asyncio.Event()
        self._register_handlers()

    def _is_owner(self, user_id: int | None) -> bool:
        return user_id == self.settings.owner_id

    async def _bot_user_id(self) -> int:
        if self._bot_id is None:
            me = await self.bot.get_me()
            self._bot_id = int(me.id)
        return self._bot_id

    async def _health(self) -> dict[str, Any]:
        store_ok = await self.store.ping()
        try:
            bridge = await self.syntx.health()
        except Exception as exc:
            bridge = {
                "ok": False,
                "session_ok": False,
                "model_available": False,
                "queue_depth": self.queue.queue_depth,
                "pages_count": 0,
                "last_error": type(exc).__name__,
            }
        return {
            "ok": store_ok,
            "session_ok": bool(bridge.get("session_ok")),
            "model_available": bool(bridge.get("model_available")),
            "queue_depth": int(bridge.get("queue_depth") or self.queue.queue_depth),
            "pages_count": int(bridge.get("pages_count") or 0),
            "last_error": bridge.get("last_error"),
            "store_ok": store_ok,
        }

    async def _typing_loop(self, chat_id: int, connection_id: str) -> None:
        while True:
            try:
                await self.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.TYPING,
                    business_connection_id=connection_id,
                )
            except Exception:
                return
            await asyncio.sleep(4)

    async def _save_connection(self, connection: BusinessConnection) -> bool:
        if connection.user.id != self.settings.owner_id:
            logger.warning(
                "Rejected business connection from unexpected owner %s",
                connection.user.id,
            )
            return False
        rights = connection.rights
        rights_json = "{}"
        if rights is not None:
            try:
                rights_json = rights.model_dump_json()
            except Exception:
                rights_json = str(
                    {
                        "can_reply": bool(getattr(rights, "can_reply", False)),
                        "can_read_messages": bool(
                            getattr(rights, "can_read_messages", False)
                        ),
                    }
                )
        await self.store.upsert_business_connection(
            business_connection_id=connection.id,
            owner_telegram_id=connection.user.id,
            enabled=bool(connection.is_enabled),
            rights_json=rights_json,
        )
        return True

    def _can_reply(self, connection: dict[str, Any]) -> bool:
        if not connection.get("enabled"):
            return False
        raw = connection.get("rights_json") or "{}"
        try:
            import json

            data = json.loads(raw) if isinstance(raw, str) else raw
            return bool(data.get("can_reply"))
        except Exception:
            return False

    async def _ensure_contact_and_conversation(
        self,
        *,
        connection_id: str,
        chat_id: int,
        telegram_user_id: int | None,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if telegram_user_id is None:
            return None
        contact = await self.store.get_or_create_contact(
            business_connection_id=connection_id,
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        await self._maybe_match_pending_name(contact)
        conversation = await self.store.get_or_create_conversation(
            business_connection_id=connection_id,
            telegram_chat_id=chat_id,
            contact_id=int(contact["id"]),
        )
        return contact, conversation

    async def _maybe_match_pending_name(self, contact: dict[str, Any]) -> None:
        full_name = normalize_person_name(
            f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}"
        )
        if not full_name:
            return
        bindings = await self.store.get_pending_bindings_by_name(full_name)
        pending = [b for b in bindings if b.get("status") == "pending"]
        if not pending:
            return
        # Multiple people with same name → never auto-bind.
        if len(pending) > 1:
            return
        binding = pending[0]
        if binding.get("candidate_telegram_user_id") and int(
            binding["candidate_telegram_user_id"]
        ) != int(contact["telegram_user_id"]):
            # Second distinct candidate — require owner decision, do not overwrite.
            await self.bot.send_message(
                self.settings.owner_id,
                (
                    f"Найден ещё один кандидат на имя «{full_name}»: "
                    f"id={contact['telegram_user_id']}. Автопривязка отменена. "
                    f"Используйте /contact_bind {contact['telegram_user_id']} {full_name}"
                ),
            )
            return
        if not binding.get("candidate_telegram_user_id"):
            await self.store.create_pending_name_binding(
                normalized_name=full_name,
                candidate_telegram_user_id=int(contact["telegram_user_id"]),
            )
            # Re-fetch / update candidate on existing row if API upserts.
            with suppress(Exception):
                await self.store.set_pending_binding_status(
                    int(binding["id"]), "pending"
                )
            await self.bot.send_message(
                self.settings.owner_id,
                (
                    f"Кандидат на профиль «{full_name}»: "
                    f"telegram_user_id={contact['telegram_user_id']}. "
                    f"Подтвердите: /contact_bind {contact['telegram_user_id']} {full_name}"
                ),
            )
            with suppress(Exception):
                await self.store.mark_pending_binding_notified(int(binding["id"]))

    async def _perform_handoff(
        self,
        *,
        conversation: dict[str, Any],
        contact: dict[str, Any],
        reason: str,
        urgency: str,
        safe_reply: str = "",
        connection_id: str | None = None,
        chat_id: int | None = None,
    ) -> None:
        await self.store.set_conversation_mode(
            int(conversation["id"]), "human", handoff_reason=reason
        )
        alert = await self.store.create_alert(
            conversation_id=int(conversation["id"]),
            level=urgency if urgency in {"normal", "urgent", "critical"} else "urgent",
            reason=reason,
        )
        recent = await self.store.get_recent_messages(
            int(conversation["id"]), limit=8
        )
        recent_lines = [
            format_dialogue_line(m, contact_label=contact_display_name(contact))
            for m in recent
        ]
        await send_owner_alert(
            self.bot,
            self.settings.owner_id,
            contact,
            reason,
            urgency if urgency in {"normal", "urgent", "critical"} else "urgent",  # type: ignore[arg-type]
            recent_lines,
            str(conversation["id"]),
            str(alert["id"]),
        )
        if urgency == "critical":
            try_send_sms_alert(
                reason=reason,
                urgency=urgency,  # type: ignore[arg-type]
                provider=self.settings.sms_provider,
                api_key=self.settings.sms_api_key,
                sms_from=self.settings.sms_from,
                owner_phone=self.settings.owner_phone,
            )
            self.alert_manager.schedule(
                bot=self.bot,
                owner_id=self.settings.owner_id,
                contact=contact,
                reason=reason,
                recent_lines=recent_lines,
                conversation_id=str(conversation["id"]),
                alert_id=str(alert["id"]),
                on_repeat=lambda: self.store.increment_alert_repeat(int(alert["id"])),
            )
        if safe_reply and connection_id and chat_id is not None:
            with suppress(Exception):
                await self._send_business_reply(
                    connection_id=connection_id,
                    chat_id=chat_id,
                    text=safe_reply,
                    conversation_id=int(conversation["id"]),
                    author_type="ai_assistant",
                )

    async def _alert_acked(self, alert_id: int) -> bool:
        # list_unacked_critical excludes acked; check via acknowledge no-op read
        rows = await self.store.list_unacked_critical()
        return all(int(r["id"]) != alert_id for r in rows)

    async def _send_business_reply(
        self,
        *,
        connection_id: str,
        chat_id: int,
        text: str,
        conversation_id: int,
        author_type: str = "ai_assistant",
    ) -> Message:
        sent = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            business_connection_id=connection_id,
            parse_mode=None,
        )
        await self.store.save_message(
            conversation_id=conversation_id,
            telegram_message_id=sent.message_id,
            author_type=author_type,
            text=text,
        )
        await self.store.increment_syntx_message_count(conversation_id)
        return sent

    async def _ask_syntx(
        self, *, prompt: str, conversation: dict[str, Any]
    ) -> AgentDecision:
        chat_url = conversation.get("syntx_chat_url")
        rotate = should_rotate_syntx_chat(conversation) or not chat_url

        async def _call() -> dict[str, Any]:
            if rotate or not chat_url:
                return await self.syntx.create_chat(
                    prompt,
                    model="Claude 4.8 Opus",
                    strict_model=True,
                )
            return await self.syntx.chat(
                prompt,
                chat_url=str(chat_url),
                model="Claude 4.8 Opus",
                strict_model=True,
            )

        try:
            result = await with_retries(
                _call,
                attempts=self.settings.syntx_max_retries,
                label="syntx",
            )
        except ModelMismatchError as exc:
            raise SyntXBridgeError(f"model mismatch: {exc}") from exc

        resolved = result.get("resolved_chat_url") or chat_url
        if resolved and (rotate or resolved != chat_url):
            await self.store.set_syntx_chat_url(
                int(conversation["id"]), str(resolved), reset_message_count=True
            )
        else:
            await self.store.increment_syntx_message_count(int(conversation["id"]))

        answer = str(result.get("answer") or "")
        try:
            return AgentDecision.from_json(answer)
        except InvalidAgentDecision:
            # Some bridges return prose+JSON; if unusable → handoff
            return AgentDecision.handoff(
                "неоднозначный или повреждённый ответ модели", urgency="urgent"
            )

    async def _process_contact_message(
        self,
        *,
        connection: dict[str, Any],
        contact: dict[str, Any],
        conversation: dict[str, Any],
        text: str,
        is_text: bool,
        telegram_message_id: int,
    ) -> None:
        connection_id = str(connection["business_connection_id"])
        chat_id = int(conversation["telegram_chat_id"])
        conversation_id = int(conversation["id"])

        # Refresh conversation (mode may have changed)
        conversation = await self.store.get_conversation_by_id(conversation_id) or conversation
        if str(conversation.get("mode")) == "human":
            logger.info("Conversation %s in human mode — AI silent", conversation_id)
            return

        profile = await get_contact_profile(
            self.store,
            int(contact["telegram_user_id"]),
            business_connection_id=connection_id,
        )
        # Session inactivity → summarize before continuing
        with suppress(Exception):
            await maybe_close_session_and_summarize(self.store, conversation)

        if is_andrey_contact(profile) or (
            str(profile.get("ai_mode") or "") == "urgent_only"
            and "андрей" in normalize_person_name(
                f"{profile.get('owner_alias') or ''} "
                f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}"
            )
            and "усенко"
            in normalize_person_name(
                f"{profile.get('owner_alias') or ''} "
                f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}"
            )
        ):
            policy = evaluate_andrey_policy(text)
            if policy.kind == "silent":
                return
            await self._perform_handoff(
                conversation=conversation,
                contact=contact,
                reason=policy.decision.handoff_reason,
                urgency=policy.decision.urgency,
                safe_reply=policy.decision.reply or SAFE_HANDOFF_ACK,
                connection_id=connection_id,
                chat_id=chat_id,
            )
            return

        if str(profile.get("ai_mode") or "") == "human":
            return
        if str(profile.get("ai_mode") or "") == "restricted":
            await self._perform_handoff(
                conversation=conversation,
                contact=contact,
                reason="контакт в режиме restricted",
                urgency="urgent",
            )
            return

        policy_reason = escalation_reason(
            text=text if is_text else None,
            is_text_message=is_text,
            runtime_paused=not self.settings.auto_reply_enabled,
            auto_reply_enabled=self.settings.auto_reply_enabled,
            context_configured=bool(
                self._knowledge.get("identity.md")
                and "[TODO:" not in (self._knowledge.get("identity.md") or "")
            ),
            contact_trusted=str(profile.get("ai_mode") or "normal")
            not in {"human", "urgent_only"},
            escalate_unknown_contacts=True,
        )

        if not is_text:
            await self._perform_handoff(
                conversation=conversation,
                contact=contact,
                reason=policy_reason or "нетекстовое сообщение",
                urgency="normal",
            )
            return

        if policy_reason and not self.settings.auto_reply_enabled:
            await self._perform_handoff(
                conversation=conversation,
                contact=contact,
                reason=policy_reason,
                urgency="normal",
            )
            return

        if not self._can_reply(connection):
            await self._perform_handoff(
                conversation=conversation,
                contact=contact,
                reason="нет права can_reply у Business-бота",
                urgency="urgent",
            )
            return

        typing_task = asyncio.create_task(
            self._typing_loop(chat_id, connection_id), name=f"typing-{chat_id}"
        )
        enqueued_at = time.monotonic()
        dialogue_key = f"{connection_id}:{chat_id}"

        async def _stale_ok() -> bool:
            current = await self.store.get_conversation_by_id(conversation_id)
            return bool(current and current.get("mode") == "ai")

        async def _job() -> None:
            rules_payload = await get_contact_communication_rules(
                self.store,
                int(contact["telegram_user_id"]),
                business_connection_id=connection_id,
            )
            rules = str(
                rules_payload.get("communication_rules")
                if isinstance(rules_payload, dict)
                else rules_payload
                or ""
            )
            memories = await get_memory_summaries(
                self.store,
                int(contact["telegram_user_id"]),
                days=self.settings.memory_retention_days,
                business_connection_id=connection_id,
            )
            recent = await self.store.get_recent_messages(
                conversation_id, limit=self.settings.history_limit
            )
            # Exclude AI replies from style; build style from owner only.
            owner_msgs = [m for m in recent if m.get("author_type") == "owner"]
            style = load_style_summary(int(contact["telegram_user_id"])) or ""
            auto_style = style_summary_from_owner_messages(owner_msgs)
            if auto_style:
                save_style_summary(int(contact["telegram_user_id"]), auto_style)
                style = auto_style

            prompt = build_claude_prompt(
                profile=profile or {},
                rules=rules,
                memory_summaries=memories,
                recent_messages=recent,
                current_message=text,
                knowledge=self._knowledge,
                style_summary=style,
                memory_days=self.settings.memory_retention_days,
            )
            try:
                decision = await self._ask_syntx(
                    prompt=prompt, conversation=conversation
                )
            except (SyntXBridgeError, ModelMismatchError, Exception) as exc:
                logger.exception("SyntX unavailable for dialogue %s", dialogue_key)
                await self._perform_handoff(
                    conversation=conversation,
                    contact=contact,
                    reason=f"SyntX недоступен или модель не подтверждена: {type(exc).__name__}",
                    urgency="critical",
                    connection_id=connection_id,
                    chat_id=chat_id,
                )
                return

            if decision.action == "silent":
                return

            must = decision.must_handoff(
                self.settings.confidence_threshold, policy_reason
            )
            if must or decision.action == "handoff":
                await self._perform_handoff(
                    conversation=conversation,
                    contact=contact,
                    reason=decision.handoff_reason or policy_reason or "handoff",
                    urgency=decision.urgency,
                    safe_reply=(
                        decision.reply
                        if decision.reply and decision.urgency in {"urgent", "critical"}
                        else ""
                    ),
                    connection_id=connection_id,
                    chat_id=chat_id,
                )
                return

            # Re-check before send (owner may have spoken / handoff)
            current = await self.store.get_conversation_by_id(conversation_id)
            if not current or current.get("mode") != "ai":
                return
            await self._send_business_reply(
                connection_id=connection_id,
                chat_id=chat_id,
                text=decision.reply,
                conversation_id=conversation_id,
            )

        try:
            await self.queue.run_for_dialogue(
                dialogue_key,
                _job,
                enqueued_at=enqueued_at,
                max_age_seconds=180.0,
                stale_check=_stale_ok,
            )
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

    async def _handle_business_message(self, message: Message, update_id: int | None) -> None:
        if not message.business_connection_id:
            return
        if update_id is not None and await self.store.is_update_processed(update_id):
            return

        connection = await self.store.get_business_connection(
            message.business_connection_id
        )
        if not connection:
            remote = await self.bot.get_business_connection(
                business_connection_id=message.business_connection_id
            )
            if not await self._save_connection(remote):
                return
            connection = await self.store.get_business_connection(
                message.business_connection_id
            )
        if not connection or int(connection["owner_telegram_id"]) != self.settings.owner_id:
            return

        bot_id = await self._bot_user_id()
        author = classify_message_author(
            message, bot_id=bot_id, owner_telegram_id=self.settings.owner_id
        )
        text, is_text = message_text(message)

        # Resolve peer user id for the chat
        peer_id = message.chat.id
        if author == "contact" and message.from_user:
            peer_id = message.from_user.id

        contact_pack = await self._ensure_contact_and_conversation(
            connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            telegram_user_id=peer_id if author == "contact" else (
                message.chat.id  # personal chat id equals peer user id in private chats
            ),
            username=message.chat.username,
            first_name=message.chat.first_name,
            last_name=message.chat.last_name,
        )
        if not contact_pack:
            return
        contact, conversation = contact_pack

        await self.store.save_message(
            conversation_id=int(conversation["id"]),
            telegram_message_id=message.message_id,
            author_type=author,
            text=text,
        )
        if update_id is not None:
            await self.store.mark_update_processed(update_id)

        if author in {"ai_assistant", "other_bot", "automatic"}:
            return

        if author == "owner":
            # Owner spoke → force human mode so AI does not interrupt.
            await self.store.set_conversation_mode(
                int(conversation["id"]), "human", handoff_reason="владелец ответил сам"
            )
            return

        # contact
        await self._process_contact_message(
            connection=connection,
            contact=contact,
            conversation=conversation,
            text=text,
            is_text=is_text,
            telegram_message_id=message.message_id,
        )

    def _register_handlers(self) -> None:
        router = self.router

        @router.business_connection()
        async def on_business_connection(connection: BusinessConnection) -> None:
            accepted = await self._save_connection(connection)
            if not accepted:
                return
            rights = connection.rights
            can_reply = bool(rights and rights.can_reply)
            await self.bot.send_message(
                self.settings.owner_id,
                f"Business-бот {'подключён' if connection.is_enabled else 'отключён'}. "
                f"Право отвечать: {'есть' if can_reply else 'нет'}.",
            )

        @router.business_message()
        async def on_business_message(message: Message) -> None:
            await self._handle_business_message(message, None)

        @router.edited_business_message()
        async def on_edited_business_message(message: Message) -> None:
            if not message.business_connection_id:
                return
            connection = await self.store.get_business_connection(
                message.business_connection_id
            )
            if not connection:
                return
            pack = await self._ensure_contact_and_conversation(
                connection_id=message.business_connection_id,
                chat_id=message.chat.id,
                telegram_user_id=message.chat.id,
                username=message.chat.username,
                first_name=message.chat.first_name,
                last_name=message.chat.last_name,
            )
            if not pack:
                return
            _contact, conversation = pack
            text, _ = message_text(message)
            await self.store.mark_message_edited(
                int(conversation["id"]), message.message_id, text=text
            )

        @router.deleted_business_messages()
        async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
            connection = await self.store.get_business_connection(
                event.business_connection_id
            )
            if not connection:
                return
            conversation = await self.store.get_or_create_conversation(
                business_connection_id=event.business_connection_id,
                telegram_chat_id=event.chat.id,
                contact_id=(
                    await self.store.get_or_create_contact(
                        business_connection_id=event.business_connection_id,
                        telegram_user_id=event.chat.id,
                        username=None,
                        first_name=None,
                        last_name=None,
                    )
                )["id"],
            )
            for mid in event.message_ids:
                await self.store.mark_message_deleted(int(conversation["id"]), mid)

        @router.message(CommandStart())
        async def owner_start(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                logger.warning("Non-owner /start from %s", message.from_user.id if message.from_user else None)
                return
            await message.answer(
                "Business-нейроагент готов.\n"
                "Подключите бота в Telegram Business и дайте can_reply.\n"
                "Команды: /status /contact_show /contact_set_mode /contact_pause_ai "
                "/contact_resume_ai /contact_bind /alert_ack"
            )

        @router.message(Command("status"))
        async def owner_status(message: Message) -> None:
            if not self._is_owner(message.from_user.id if message.from_user else None):
                logger.warning("Rejected admin command from non-owner")
                return
            try:
                health = await self.syntx.health()
            except Exception as exc:
                health = {"ok": False, "last_error": type(exc).__name__}
            await message.answer(
                "Статус:\n"
                f"• auto_reply: {self.settings.auto_reply_enabled}\n"
                f"• syntx ok: {health.get('ok')}\n"
                f"• session_ok: {health.get('session_ok')}\n"
                f"• model_available: {health.get('model_available')}\n"
                f"• queue_depth: {self.queue.queue_depth}\n"
                f"• pages_count: {health.get('pages_count')}\n"
                f"• last_error: {health.get('last_error')}"
            )

        async def _require_owner(message: Message) -> bool:
            if self._is_owner(message.from_user.id if message.from_user else None):
                return True
            logger.warning(
                "Rejected admin command from user_id=%s",
                message.from_user.id if message.from_user else None,
            )
            await message.answer("Недостаточно прав.")
            return False

        @router.message(Command("contact_show"))
        async def contact_show(message: Message) -> None:
            if not await _require_owner(message):
                return
            parts = (message.text or "").split()
            if len(parts) < 2:
                await message.answer("Формат: /contact_show <telegram_user_id>")
                return
            try:
                uid = int(parts[1])
            except ValueError:
                await message.answer("telegram_user_id должен быть числом")
                return
            profile = await get_contact_profile(self.store, uid)
            if not profile:
                await message.answer("Контакт не найден.")
                return
            await message.answer(
                "\n".join(
                    f"{k}: {profile.get(k)}"
                    for k in (
                        "telegram_user_id",
                        "owner_alias",
                        "importance",
                        "relationship",
                        "ai_mode",
                        "preferred_tone",
                        "manual_owner_notes",
                    )
                )
            )

        @router.message(Command("contact_set_alias", "contact_set_importance", "contact_set_relation", "contact_set_mode", "contact_add_note", "contact_add_rule"))
        async def contact_set(message: Message) -> None:
            if not await _require_owner(message):
                return
            parts = (message.text or "").split(maxsplit=2)
            cmd = parts[0].split("@", 1)[0].lstrip("/")
            if len(parts) < 3:
                await message.answer(f"Формат: /{cmd} <telegram_user_id> <значение>")
                return
            try:
                uid = int(parts[1])
            except ValueError:
                await message.answer("telegram_user_id должен быть числом")
                return
            contact = await self.store.get_contact_by_telegram_id(uid)
            if not contact:
                await message.answer("Контакт не найден.")
                return
            value = parts[2].strip()
            field_map = {
                "contact_set_alias": "owner_alias",
                "contact_set_importance": "importance",
                "contact_set_relation": "relationship",
                "contact_set_mode": "ai_mode",
                "contact_add_note": "manual_owner_notes",
                "contact_add_rule": "communication_rules",
            }
            field = field_map[cmd]
            if field in {"manual_owner_notes", "communication_rules"}:
                prev = str(contact.get(field) or "")
                value = (prev + "\n" + value).strip() if prev else value
            await self.store.update_contact_fields(int(contact["id"]), **{field: value})
            await message.answer("Сохранено.")

        @router.message(Command("contact_delete_note"))
        async def contact_delete_note(message: Message) -> None:
            if not await _require_owner(message):
                return
            parts = (message.text or "").split()
            if len(parts) < 2:
                await message.answer("Формат: /contact_delete_note <telegram_user_id>")
                return
            uid = int(parts[1])
            contact = await self.store.get_contact_by_telegram_id(uid)
            if not contact:
                await message.answer("Контакт не найден.")
                return
            await self.store.update_contact_fields(
                int(contact["id"]), manual_owner_notes=""
            )
            await message.answer("Заметки удалены.")

        @router.message(Command("contact_pause_ai", "contact_resume_ai"))
        async def contact_pause_resume(message: Message) -> None:
            if not await _require_owner(message):
                return
            parts = (message.text or "").split()
            cmd = parts[0].split("@", 1)[0].lstrip("/")
            if len(parts) < 2:
                await message.answer(f"Формат: /{cmd} <telegram_user_id>")
                return
            uid = int(parts[1])
            contact = await self.store.get_contact_by_telegram_id(uid)
            if not contact:
                await message.answer("Контакт не найден.")
                return
            conv = await self.store.get_conversation_for_contact(int(contact["id"]))
            if not conv:
                await message.answer("Диалог не найден.")
                return
            if cmd == "contact_pause_ai":
                await self.store.set_conversation_mode(
                    int(conv["id"]), "human", handoff_reason="пауза владельца"
                )
                await message.answer("AI поставлен на паузу для контакта.")
            else:
                await self.store.set_conversation_mode(int(conv["id"]), "ai")
                await message.answer("AI возобновлён для контакта.")

        @router.message(Command("contact_bind"))
        async def contact_bind(message: Message) -> None:
            if not await _require_owner(message):
                return
            parts = (message.text or "").split(maxsplit=2)
            if len(parts) < 3:
                await message.answer(
                    "Формат: /contact_bind <telegram_user_id> <Нормализованное Имя>"
                )
                return
            uid = int(parts[1])
            name = normalize_person_name(parts[2])
            bindings = await self.store.get_pending_bindings_by_name(name)
            if not bindings:
                await self.store.create_pending_name_binding(
                    normalized_name=name, candidate_telegram_user_id=uid
                )
                bindings = await self.store.get_pending_bindings_by_name(name)
            for b in bindings:
                await self.store.set_pending_binding_status(int(b["id"]), "confirmed")
            contact = await self.store.get_contact_by_telegram_id(uid)
            if contact and contact.get("business_connection_id"):
                await apply_andrey_usenko_defaults(
                    self.store,
                    uid,
                    business_connection_id=str(contact["business_connection_id"]),
                )
            elif contact:
                await self.store.update_contact_fields(
                    int(contact["id"]),
                    owner_alias=parts[2].strip(),
                    importance="important",
                    relationship="работа",
                    ai_mode="urgent_only",
                )
            await message.answer(f"Привязка подтверждена: {uid} → {name}")

        @router.message(Command("contact_unbind"))
        async def contact_unbind(message: Message) -> None:
            if not await _require_owner(message):
                return
            parts = (message.text or "").split(maxsplit=2)
            if len(parts) < 3:
                await message.answer(
                    "Формат: /contact_unbind <telegram_user_id> <имя>"
                )
                return
            uid = int(parts[1])
            name = normalize_person_name(parts[2])
            for b in await self.store.get_pending_bindings_by_name(name):
                if int(b.get("candidate_telegram_user_id") or 0) == uid:
                    await self.store.set_pending_binding_status(int(b["id"]), "rejected")
            await message.answer("Привязка снята.")

        @router.message(Command("alert_ack"))
        async def alert_ack_cmd(message: Message) -> None:
            if not await _require_owner(message):
                return
            parts = (message.text or "").split()
            if len(parts) < 2:
                await message.answer("Формат: /alert_ack <alert_id>")
                return
            alert_id = int(parts[1])
            await self.store.acknowledge_alert(alert_id)
            self.alert_manager.acknowledge(str(alert_id))
            await message.answer("Алерт подтверждён.")

        @router.callback_query()
        async def owner_callback(callback: CallbackQuery) -> None:
            if not self._is_owner(callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            data = callback.data or ""
            if data.startswith("ack:"):
                alert_id = data.split(":", 1)[1]
                with suppress(Exception):
                    await self.store.acknowledge_alert(int(alert_id))
                self.alert_manager.acknowledge(alert_id)
                await callback.answer("Принято.")
                return
            if data.startswith("resume_ai:"):
                conv_id = int(data.split(":", 1)[1])
                await self.store.set_conversation_mode(conv_id, "ai")
                await callback.answer("AI возвращён.")
                return
            await callback.answer("Неизвестная кнопка.")

    async def run(self) -> None:
        web_runner = None
        try:
            await self.store.connect()
            await seed_pending_andrey_usenko_profile(self.store)
            with suppress(Exception):
                await self.store.prune_old_working_memory(
                    self.settings.memory_retention_days
                )
            web_runner = await start_webserver(self.settings.port, self._health)
            dispatcher = Dispatcher()
            dispatcher.include_router(self.router)

            # Capture update_id via middleware-like outer handler
            @dispatcher.update.outer_middleware()
            async def dedupe_middleware(handler, event: Update, data):
                if event.update_id is not None:
                    if await self.store.is_update_processed(event.update_id):
                        return None
                result = await handler(event, data)
                # Business messages mark themselves; still mark generic updates lightly
                if event.business_message and event.update_id is not None:
                    # already marked inside handler; ok
                    pass
                return result

            await self.bot.delete_webhook(drop_pending_updates=False)
            logger.info("Telegram Business neuroagent started")
            await dispatcher.start_polling(
                self.bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                handle_as_tasks=False,
                close_bot_session=False,
            )
        finally:
            self._stopping.set()
            self.alert_manager.shutdown()
            if web_runner:
                with suppress(Exception):
                    await web_runner.cleanup()
            with suppress(Exception):
                await self.syntx.close()
            with suppress(Exception):
                await self.store.close()
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
