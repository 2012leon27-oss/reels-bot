# grok_client.py
# Подключение к Groq API через OpenAI-совместимый клиент
# (Groq — это НЕ Grok от xAI, а отдельный сервис!)

import os
from openai import AsyncOpenAI
from prompts import SYSTEM_PROMPT

# Инициализация клиента Groq
client = AsyncOpenAI(
    api_key=os.getenv("GROK_API_KEY"),
       base_url="https://openrouter.ai/api/v1"
)

MODEL = os.getenv("GROK_MODEL", "deepseek/deepseek-chat-v3.1:free")


async def ask_grok(user_message: str, history: list = None) -> str:
    """
    Отправляет запрос в Groq и возвращает ответ.
    
    user_message — текущее сообщение пользователя
    history — список предыдущих сообщений [{"role": "user/assistant", "content": "..."}]
    """
    if history is None:
        history = []
    
    # Собираем сообщения: системный промпт + история + новое сообщение
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=2000,
        )
        return response.choices[0].message.content
    
    except Exception as e:
        print(f"❌ Ошибка Groq API: {e}")
        return "ой, что-то я завис) попробуй ещё раз через пару секунд"
