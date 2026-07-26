СЮДА КЛАДЁШЬ КЛЮЧ САМ (файлы не уходят в Git)

1) api.key          — твой API-ключ (одна строка, без кавычек)
2) base_url.txt     — адрес API (одна строка). Примеры:
   https://openrouter.ai/api/v1
   https://api.x.ai/v1
3) model.txt        — имя модели (одна строка). Примеры:
   x-ai/grok-4.5        (OpenRouter)
   grok-4.5             (xAI напрямую)

После сохранения файлов запусти:
  ./start_local.sh
или:
  python3 bot.py

Открыть в браузере: http://localhost:10000

Заметки сохраняются в папку notes/
