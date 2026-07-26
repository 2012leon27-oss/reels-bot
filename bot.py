"""Process entry point: web application plus optional Telegram bot."""

from __future__ import annotations

import asyncio
import html
import io
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from auth import telegram_user_allowed
from config import settings
from database import create_note
from grok_client import render_markdown, structure_thoughts, transcribe_audio
from web_app import create_app

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s — %(levelname)s — %(name)s — %(message)s"
)
logger = logging.getLogger(__name__)
dispatcher = Dispatcher()


def mini_app_keyboard() -> InlineKeyboardMarkup | None:
    if not settings.app_base_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть Архитектор мыслей",
                    web_app=WebAppInfo(url=settings.app_base_url),
                )
            ]
        ]
    )


def note_link(note_id: str) -> str:
    if not settings.app_base_url:
        return ""
    return f"{settings.app_base_url}/?note={note_id}"


def note_keyboard(note_id: str) -> InlineKeyboardMarkup | None:
    url = note_link(note_id)
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть полную заметку",
                    web_app=WebAppInfo(url=url),
                )
            ]
        ]
    )


def message_allowed(message: Message) -> bool:
    return bool(
        message.from_user and telegram_user_allowed(int(message.from_user.id))
    )


async def reject_unlisted(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "unknown"
    await message.answer(
        "У этого аккаунта нет доступа. Администратор может добавить ваш Telegram ID "
        f"<code>{user_id}</code> в TELEGRAM_ALLOWED_USER_IDS."
    )


async def process_text(message: Message, source_text: str) -> None:
    if not message_allowed(message):
        await reject_unlisted(message)
        return
    if len(source_text) > settings.max_text_chars:
        await message.answer(
            f"Запись слишком длинная. Максимум — {settings.max_text_chars:,} символов."
        )
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    status = await message.answer("Сохраняю все смыслы и собираю структуру…")
    try:
        structured = await structure_thoughts(source_text)
        markdown = render_markdown(structured, source_text)
        note = await create_note(
            owner_id=f"telegram:{message.from_user.id}",
            source_text=source_text,
            structured=structured,
            structured_markdown=markdown,
            mode="auto",
        )
        link = note_link(note["id"])
        raw_summary = str(structured.get("summary") or "Готово.")[:1200]
        summary = html.escape(raw_summary)
        title = html.escape(structured["title"])
        response = f"<b>{title}</b>\n\n{summary}"
        if link:
            response += "\n\nПолный документ сохранён в приложении."
        else:
            body = html.escape(str(structured["structured_text"])[:2400])
            response += f"\n\n{body}"
        await status.edit_text(response, reply_markup=note_keyboard(note["id"]))
    except Exception:
        logger.exception("Could not structure Telegram message")
        await status.edit_text(
            "Не удалось обработать запись. Проверьте настройки API и попробуйте ещё раз."
        )


@dispatcher.message(CommandStart())
async def start(message: Message) -> None:
    if not message_allowed(message):
        await reject_unlisted(message)
        return
    text = (
        "<b>Архитектор мыслей</b>\n\n"
        "Пришли текст или голосовое. Я сохраню исходник, разложу мысль по смысловым "
        "блокам, выделю решения, задачи, идеи и открытые вопросы — без выдуманных деталей."
    )
    if not settings.app_base_url:
        text += "\n\nАдминистратору: задайте APP_BASE_URL, чтобы включить Mini App."
    await message.answer(text, reply_markup=mini_app_keyboard())


@dispatcher.message(Command("app"))
async def app_command(message: Message) -> None:
    if not message_allowed(message):
        await reject_unlisted(message)
        return
    if mini_app_keyboard():
        await message.answer("Откройте приложение:", reply_markup=mini_app_keyboard())
    else:
        await message.answer("Ссылка приложения ещё не настроена.")


@dispatcher.message(Command("help"))
async def help_command(message: Message) -> None:
    if not message_allowed(message):
        await reject_unlisted(message)
        return
    await message.answer(
        "Отправьте обычное сообщение, голосовое или аудиофайл. "
        "Оригинал всегда останется внутри заметки, а результат можно открыть по ссылке."
    )


@dispatcher.message(F.voice | F.audio)
async def audio_message(message: Message) -> None:
    if not message_allowed(message):
        await reject_unlisted(message)
        return
    attachment = message.voice or message.audio
    if not attachment:
        return
    telegram_limit = min(settings.max_audio_bytes, 20 * 1024 * 1024)
    if attachment.file_size and attachment.file_size > telegram_limit:
        await message.answer("Аудиофайл слишком большой. Для Telegram максимум — 20 МБ.")
        return

    status = await message.answer("Расшифровываю запись…")
    try:
        file = await message.bot.get_file(attachment.file_id)
        destination = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=destination)
        filename = getattr(message.audio, "file_name", None) or "telegram-voice.ogg"
        content_type = getattr(message.audio, "mime_type", None) or "audio/ogg"
        transcript = await transcribe_audio(
            destination.getvalue(), filename=filename, content_type=content_type
        )
        await status.delete()
        await process_text(message, transcript)
    except Exception:
        logger.exception("Could not process Telegram audio")
        await status.edit_text(
            "Не удалось расшифровать запись. Попробуйте отправить аудио ещё раз."
        )


@dispatcher.message(F.text)
async def text_message(message: Message) -> None:
    await process_text(message, message.text.strip())


async def run() -> None:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.port)
    await site.start()
    logger.info("Web application is listening on port %s", settings.port)

    try:
        if settings.bot_token:
            bot = Bot(
                settings.bot_token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                await dispatcher.start_polling(bot)
            finally:
                await bot.session.close()
        else:
            logger.warning("BOT_TOKEN is not configured; running web application only")
            await asyncio.Future()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
