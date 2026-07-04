# AGENTS.md

## Cursor Cloud specific instructions

### What this is
A Telegram bot ("нейрокуратор" for the REELS PRODUCER course). Single Python service:
- `bot.py` — entry point, aiogram long-polling + handlers.
- `grok_client.py` — LLM calls via the Groq API using the OpenAI-compatible client.
- `database.py` — PostgreSQL access (users + chat history) via `asyncpg`.
- `keep_alive.py` — small aiohttp web server (`/`, `/health`) so Render's free tier stays awake.
- `prompts.py` — the system prompt.

There is no build step, no lint config, and no automated test suite in this repo.

### Python env
- The update script creates a virtualenv at `.venv` and installs `requirements.txt`. Activate it before running anything: `source .venv/bin/activate`.
- `runtime.txt` pins Python 3.11 (that is a Render hint); the code also runs fine on the system Python 3.12 in this VM.

### PostgreSQL (required for `database.py` and the bot)
- PostgreSQL 16 is installed system-wide but is NOT auto-started. Start it each session with `sudo pg_ctlcluster 16 main start`.
- A local dev DB is created as `reelsbot` (user `postgres` / password `postgres`). Recreate if missing:
  `sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';"` and `sudo -u postgres psql -c "CREATE DATABASE reelsbot;"`.
- Use `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/reelsbot`.
- `init_db()` creates tables/indexes on startup, so no manual migration is needed.

### Environment variables (gotchas)
Copy `env.example` for reference, but note two mismatches in that file:
- The code reads `GROK_API_KEY` (with a K), while `env.example` names it `GROG_API_KEY` — set `GROK_API_KEY`.
- The committed sample `GROK_MODEL=grog-2-latest` is not a valid Groq model. `grok_client.py` defaults to `llama-3.3-70b-versatile`; use a real Groq model.
- `GROK_API_KEY` is a Groq key (`https://api.groq.com`), not an xAI Grok key. The key committed in `env.example` is invalid (returns 401).
- Required to run the full bot: `BOT_TOKEN` (Telegram), `GROK_API_KEY` (Groq), `DATABASE_URL`. Optional: `GROK_MODEL`, `ADMIN_ID`, `PORT` (default 10000).

### Running
- Full bot (needs a valid `BOT_TOKEN` + `GROK_API_KEY` + running Postgres): `python bot.py`. It long-polls Telegram and also serves the keep-alive web server on `PORT`.
- Individual layers can be exercised without Telegram/Groq: importing `database.py` against the local `reelsbot` DB, and calling `keep_alive.start_webserver()` for the health endpoints.
