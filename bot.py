# bot.py

import os
import asyncio
import logging
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from grok_client import ask_grok
from database import (
    init_db, save_user, save_message,
    get_history, clear_history, get_stats
)
from keep_alive import start_webserver

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
MINI_APP_URL = "https://seflev299-cmyk.github.io/reels-producer-ai/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def get_mini_app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥜 Открыть REELS PRODUCER", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await save_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        first_name=message.from_user.first_name or ""
    )
    greeting = (
        "привет! я нейрокуратор курса <b>«Книга рецептов вкусного контента»</b> от Шеф Льва)\n\n"
        "я помогу тебе разобраться в материале, напомню нужный блок, помогу придумать идеи, "
        "написать сценарий, подобрать кадры и ракурсы — в общем, всё, что есть в курсе\n\n"
        "могу дать обратную связь на твой сценарий или раскадровку. видео анализировать не умею — "
        "но если скинешь текст + описание кадров + скриншоты — разберу всё по полочкам\n\n"
        "если нужно что-то за пределами курса (продажи, воронки, монетизация) — "
        "честно скажу, что во мне этих данных нет\n\n"
        "👇 жми на кнопку ниже, чтобы открыть <b>REELS PRODUCER.AI</b> — "
        "там удобный интерфейс для работы\n\n"
        "<i>команды:</i>\n"
        "/app — открыть Mini App\n"
        "/reset — очистить историю диалога\n"
        "/help — помощь"
    )
    await message.answer(greeting, reply_markup=get_mini_app_keyboard())


@dp.message(Command("app"))
async def cmd_app(message: Message):
    await message.answer(
        "🥜 жми на кнопку, чтобы открыть <b>REELS PRODUCER.AI</b>",
        reply_markup=get_mini_app_keyboard()
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "<b>что я умею:</b>\n\n"
        "🥩 объяснить любой блок курса\n"
        "🍳 помочь придумать идеи для рилс\n"
        "📝 написать сценарий по любой из 7 структур\n"
        "🌶 подобрать специи под твой рилс\n"
        "🎬 помочь с кадрами, ракурсами, композициями\n"
        "📊 дать обратную связь на твой хук/сценарий\n\n"
        "<b>команды:</b>\n"
        "/start — начать заново\n"
        "/app — открыть Mini App\n"
        "/reset — очистить историю\n"
        "/help — это сообщение"
    )
    await message.answer(text, reply_markup=get_mini_app_keyboard())


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    await clear_history(message.from_user.id)
    await message.answer("история очищена) начнём с чистого листа. о чём поговорим?")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    stats = await get_stats()
    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"💬 Всего сообщений: <b>{stats['total_messages']}</b>\n"
        f"🔥 Активны за сутки: <b>{stats['active_today']}</b>"
    )
    await message.answer(text)


@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    user_text = message.text
    await save_user(
        user_id=user_id,
        username=message.from_user.username or "",
        first_name=message.from_user.first_name or ""
    )
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    history = await get_history(user_id, limit=20)
    answer = await ask_grok(user_text, history=history)
    await save_message(user_id, "user", user_text)
    await save_message(user_id, "assistant", answer)
    try:
        await message.answer(answer)
    except Exception as e:
        logger.warning(f"Ошибка форматирования: {e}")
        await message.answer(answer, parse_mode=None)


@dp.message(F.video | F.video_note | F.animation)
async def handle_video(message: Message):
    text = (
        "к сожалению, я не умею анализировать видео — нейросети пока реально плохо с этим справляются\n\n"
        "но я могу дать тебе полноценную обратную связь, если скинешь:\n\n"
        "1. <b>сценарий</b> — полный текст того, что ты говоришь в рилсе\n"
        "2. <b>раскадровку</b> — какие кадры, ракурсы, композиции и в каком порядке\n"
        "3. <b>скриншоты</b> — 3-5 кадров из разных моментов\n\n"
        "чем больше из этого скинешь — тем точнее будет обратная связь)"
    )
    await message.answer(text)


@dp.message(F.photo)
async def handle_photo(message: Message):
    text = (
        "вижу скриншот) опиши коротко — это кадр из твоего рилса? или что-то другое?\n\n"
        "если хочешь обратную связь — скинь ещё сценарий и описание раскадровки, "
        "тогда смогу разобрать всё по полочкам"
    )
    await message.answer(text)


async def main():
    logger.info("🚀 Запуск бота...")
    await init_db()
    await start_webserver()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Бот запущен и готов к работе")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
