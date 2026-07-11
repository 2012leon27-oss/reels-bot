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


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    owner_id: int
    database_url: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    confidence_threshold: float
    history_limit: int
    llm_timeout_seconds: float
    llm_max_retries: int
    auto_reply_enabled: bool
    escalate_unknown_contacts: bool
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        required = {
            "BOT_TOKEN": os.getenv("BOT_TOKEN", "").strip(),
            "DATABASE_URL": os.getenv("DATABASE_URL", "").strip(),
            "LLM_API_KEY": os.getenv("LLM_API_KEY", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        owner_id = _as_int("OWNER_ID", 0)
        if owner_id <= 0:
            raise ConfigurationError("OWNER_ID must be your positive Telegram user ID")

        threshold = _as_float("CONFIDENCE_THRESHOLD", 0.82)
        if not 0.0 <= threshold <= 1.0:
            raise ConfigurationError("CONFIDENCE_THRESHOLD must be between 0 and 1")

        history_limit = _as_int("HISTORY_LIMIT", 30)
        if not 1 <= history_limit <= 100:
            raise ConfigurationError("HISTORY_LIMIT must be between 1 and 100")

        retries = _as_int("LLM_MAX_RETRIES", 3)
        if not 1 <= retries <= 5:
            raise ConfigurationError("LLM_MAX_RETRIES must be between 1 and 5")

        timeout = _as_float("LLM_TIMEOUT_SECONDS", 30.0)
        if timeout <= 0:
            raise ConfigurationError("LLM_TIMEOUT_SECONDS must be positive")

        port = _as_int("PORT", 10000)
        if not 1 <= port <= 65535:
            raise ConfigurationError("PORT must be between 1 and 65535")

        return cls(
            bot_token=required["BOT_TOKEN"],
            owner_id=owner_id,
            database_url=required["DATABASE_URL"],
            llm_api_key=required["LLM_API_KEY"],
            llm_base_url=os.getenv(
                "LLM_BASE_URL", "https://api.groq.com/openai/v1"
            ).rstrip("/"),
            llm_model=os.getenv(
                "LLM_MODEL", "llama-3.3-70b-versatile"
            ).strip(),
            confidence_threshold=threshold,
            history_limit=history_limit,
            llm_timeout_seconds=timeout,
            llm_max_retries=retries,
            auto_reply_enabled=_as_bool("AUTO_REPLY_ENABLED", False),
            escalate_unknown_contacts=_as_bool(
                "ESCALATE_UNKNOWN_CONTACTS", True
            ),
            port=port,
        )
