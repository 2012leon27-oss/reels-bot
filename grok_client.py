# grok_client.py
# Подключение к OpenRouter API через OpenAI-совместимый клиент
# OpenRouter — агрегатор моделей, даёт доступ к DeepSeek бесплатно

import os
from openai import AsyncOpenAI
from prompts import SYSTEM_PROMPT

# Инициализация клиента OpenRouter
client = AsyncOpenAI(
    api_key=os.getenv("GROK_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

MODEL = os.getenv("GROK_MODEL", "meta-llama/llama-3.3-70b-instruct:free")


async def ask_grok(user_message: str, history: list = None) -> str:
    """
    Отправляет запрос в OpenRouter и возвращает ответ.
    """
    if history is None:
        history = []
    
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
        print(f"❌ Ошибка OpenRouter API: {e}")
        return "ой, что-то я завис) попробуй ещё раз через пару секунд"
