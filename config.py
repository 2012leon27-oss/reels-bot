"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    owner_id: int
    database_url: str
    syntx_bridge_url: str
    confidence_threshold: float
    history_limit: int
    memory_retention_days: int
    session_inactivity_minutes: int
    syntx_chat_max_messages: int
    syntx_chat_rotation_hours: float
    syntx_timeout_seconds: float
    syntx_max_retries: int
    auto_reply_enabled: bool
    alert_channels: tuple[str, ...]
    sms_provider: str
    sms_api_key: str
    sms_from: str
    owner_phone: str
    push_webhook_url: str
    port: int
    # Legacy Groq path (optional fallback for offline unit tests only)
    llm_api_key: str
    llm_base_url: str
    llm_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        bot_token = _first_env("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
        database_url = _first_env("DATABASE_URL", default="sqlite:///data/bot.db")
        if not bot_token:
            raise ConfigurationError(
                "Missing TELEGRAM_BOT_TOKEN (or BOT_TOKEN)"
            )
        if not database_url:
            raise ConfigurationError("Missing DATABASE_URL")

        owner_raw = _first_env("OWNER_TELEGRAM_ID", "OWNER_ID", default="0")
        try:
            owner_id = int(owner_raw)
        except ValueError as exc:
            raise ConfigurationError(
                "OWNER_TELEGRAM_ID must be your positive Telegram user ID"
            ) from exc
        if owner_id <= 0:
            raise ConfigurationError(
                "OWNER_TELEGRAM_ID must be your positive Telegram user ID"
            )

        threshold = _as_float("CONFIDENCE_THRESHOLD", 0.75)
        if not 0.0 <= threshold <= 1.0:
            raise ConfigurationError("CONFIDENCE_THRESHOLD must be between 0 and 1")

        history_limit = _as_int("RECENT_MESSAGE_LIMIT", 30)
        if not 1 <= history_limit <= 100:
            raise ConfigurationError("RECENT_MESSAGE_LIMIT must be between 1 and 100")

        retries = _as_int("SYNTX_MAX_RETRIES", 3)
        if not 1 <= retries <= 5:
            raise ConfigurationError("SYNTX_MAX_RETRIES must be between 1 and 5")

        timeout = _as_float("SYNTX_TIMEOUT_SECONDS", 180.0)
        if timeout <= 0:
            raise ConfigurationError("SYNTX_TIMEOUT_SECONDS must be positive")

        port = _as_int("PORT", 10000)
        if not 1 <= port <= 65535:
            raise ConfigurationError("PORT must be between 1 and 65535")

        channels = tuple(
            part.strip().lower()
            for part in _first_env("ALERT_CHANNELS", default="telegram").split(",")
            if part.strip()
        ) or ("telegram",)

        return cls(
            bot_token=bot_token,
            owner_id=owner_id,
            database_url=database_url,
            syntx_bridge_url=_first_env(
                "SYNTX_BRIDGE_URL", default="http://127.0.0.1:8000/syntx_chat"
            ),
            confidence_threshold=threshold,
            history_limit=history_limit,
            memory_retention_days=_as_int("MEMORY_RETENTION_DAYS", 15),
            session_inactivity_minutes=_as_int("SESSION_INACTIVITY_MINUTES", 30),
            syntx_chat_max_messages=_as_int("SYNTX_CHAT_MAX_MESSAGES", 40),
            syntx_chat_rotation_hours=_as_float("SYNTX_CHAT_ROTATION_HOURS", 24.0),
            syntx_timeout_seconds=timeout,
            syntx_max_retries=retries,
            auto_reply_enabled=_as_bool("AUTO_REPLY_ENABLED", False),
            alert_channels=channels,
            sms_provider=_first_env("SMS_PROVIDER"),
            sms_api_key=_first_env("SMS_API_KEY"),
            sms_from=_first_env("SMS_FROM"),
            owner_phone=_first_env("OWNER_PHONE"),
            push_webhook_url=_first_env("PUSH_WEBHOOK_URL"),
            port=port,
            llm_api_key=_first_env("LLM_API_KEY"),
            llm_base_url=_first_env(
                "LLM_BASE_URL", default="https://api.groq.com/openai/v1"
            ).rstrip("/"),
            llm_model=_first_env("LLM_MODEL", default="llama-3.3-70b-versatile"),
        )
