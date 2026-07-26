"""Runtime configuration for the Thought Architect application."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
SECRETS_DIR = ROOT / "secrets"


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _read_secret_file(name: str) -> str:
    path = SECRETS_DIR / name
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _int_list(value: str) -> tuple[int, ...]:
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if item:
            result.append(int(item))
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    bot_token: str = _first_env("BOT_TOKEN")
    admin_id: int = int(_first_env("ADMIN_ID", default="0"))
    telegram_allowed_user_ids: tuple[int, ...] = _int_list(
        _first_env("TELEGRAM_ALLOWED_USER_IDS")
    )
    database_url: str = _first_env("DATABASE_URL")
    sqlite_path: str = _first_env("SQLITE_PATH", default="data/thoughts.db")

    ai_api_key: str = _first_env(
        "AI_API_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "GROK_API_KEY",
        "GROG_API_KEY",
        "XAI_API_KEY",
    ) or _read_secret_file("api.key")
    ai_base_url: str = (
        _first_env("AI_BASE_URL")
        or _read_secret_file("base_url.txt")
        or "https://openrouter.ai/api/v1"
    )
    ai_model: str = (
        _first_env("AI_MODEL", "GROQ_MODEL", "GROK_MODEL", "GROG_MODEL")
        or _read_secret_file("model.txt")
        or "x-ai/grok-4.5"
    )
    transcription_api_key: str = _first_env(
        "TRANSCRIPTION_API_KEY",
        "GROQ_API_KEY",
        "GROK_API_KEY",
        "GROG_API_KEY",
    ) or _read_secret_file("transcription.key")
    transcription_base_url: str = _first_env(
        "TRANSCRIPTION_BASE_URL", default="https://api.groq.com/openai/v1"
    )
    transcription_model: str = _first_env(
        "TRANSCRIPTION_MODEL", default="whisper-large-v3-turbo"
    )

    app_base_url: str = _first_env("APP_BASE_URL", "RENDER_EXTERNAL_URL").rstrip("/")
    app_access_key: str = _first_env("APP_ACCESS_KEY")
    app_env: str = _first_env("APP_ENV", default="development").lower()
    port: int = int(_first_env("PORT", default="10000"))
    max_text_chars: int = int(_first_env("MAX_TEXT_CHARS", default="50000"))
    max_audio_bytes: int = int(
        _first_env("MAX_AUDIO_BYTES", default=str(25 * 1024 * 1024))
    )
    notes_output_dir: str = _first_env("NOTES_OUTPUT_DIR", default="notes")

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @property
    def allowed_telegram_ids(self) -> set[int]:
        result = set(self.telegram_allowed_user_ids)
        if self.admin_id:
            result.add(self.admin_id)
        return result


settings = Settings()
