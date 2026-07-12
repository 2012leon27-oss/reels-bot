"""SyntX bridge and browser automation settings.

CSS selectors target the SyntX web UI. After the first manual login in the
persistent browser profile, the live DOM may differ from defaults — override
individual selectors via ``SYNTX_SELECTOR_*`` environment variables and verify
each one in DevTools before relying on automation in production.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# UUID segment in SyntX chat URLs after a conversation is created.
CHAT_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


class SyntXConfigError(RuntimeError):
    """Raised when SyntX configuration is missing or invalid."""


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SyntXConfigError(f"{name} must be true or false")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise SyntXConfigError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise SyntXConfigError(f"{name} must be a number") from exc


@dataclass(frozen=True, slots=True)
class SyntXSelectors:
    """DOM selectors for the SyntX chat UI (override via SYNTX_SELECTOR_*)."""

    textarea: str
    send_button: str
    model_selector: str
    model_display: str
    assistant_message: str
    login_indicator: str

    @classmethod
    def from_env(cls) -> SyntXSelectors:
        return cls(
            textarea=_env_str(
                "SYNTX_SELECTOR_TEXTAREA",
                'textarea[placeholder*="message"], textarea[data-testid="chat-input"], textarea',
            ),
            send_button=_env_str(
                "SYNTX_SELECTOR_SEND_BUTTON",
                'button[data-testid="send-button"], button[aria-label*="Send"], button[type="submit"]',
            ),
            model_selector=_env_str(
                "SYNTX_SELECTOR_MODEL_SELECTOR",
                'button[data-testid="model-selector"], [aria-label*="model"], .model-selector',
            ),
            model_display=_env_str(
                "SYNTX_SELECTOR_MODEL_DISPLAY",
                '[data-testid="model-name"], .model-name, .selected-model',
            ),
            assistant_message=_env_str(
                "SYNTX_SELECTOR_ASSISTANT_MESSAGE",
                '[data-role="assistant"], [data-assistant="true"], .assistant-message',
            ),
            login_indicator=_env_str(
                "SYNTX_SELECTOR_LOGIN_INDICATOR",
                'input[type="password"], form[action*="login"], [data-testid="login"]',
            ),
        )


@dataclass(frozen=True, slots=True)
class SyntXSettings:
    bridge_host: str
    bridge_port: int
    bridge_url: str
    base_url: str
    new_chat_url: str
    model_name: str
    browser_profile_dir: Path
    headless: bool
    page_limit: int
    page_idle_seconds: float
    request_timeout: float
    selectors: SyntXSelectors

    @property
    def new_chat_full_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.new_chat_url.lstrip('/')}"

    @classmethod
    def from_env(cls) -> SyntXSettings:
        load_dotenv()

        host = _env_str("SYNTX_BRIDGE_HOST", "127.0.0.1")
        port = _env_int("SYNTX_BRIDGE_PORT", 8000)
        bridge_url = _env_str(
            "SYNTX_BRIDGE_URL",
            f"http://{host}:{port}/syntx_chat",
        )

        profile_raw = _env_str("SYNTX_BROWSER_PROFILE_DIR", "./syntx_browser_profile")
        profile_dir = Path(profile_raw).expanduser().resolve()

        page_limit = _env_int("SYNTX_PAGE_LIMIT", 30)
        if page_limit < 1:
            raise SyntXConfigError("SYNTX_PAGE_LIMIT must be at least 1")

        idle_seconds = _env_float("SYNTX_PAGE_IDLE_SECONDS", 1800.0)
        if idle_seconds <= 0:
            raise SyntXConfigError("SYNTX_PAGE_IDLE_SECONDS must be positive")

        timeout = _env_float("SYNTX_REQUEST_TIMEOUT", 180.0)
        if timeout <= 0:
            raise SyntXConfigError("SYNTX_REQUEST_TIMEOUT must be positive")

        return cls(
            bridge_host=host,
            bridge_port=port,
            bridge_url=bridge_url,
            base_url=_env_str("SYNTX_BASE_URL", "https://syntx.ai").rstrip("/"),
            new_chat_url=_env_str("SYNTX_NEW_CHAT_URL", "/chat/new"),
            model_name=_env_str("SYNTX_MODEL_NAME", "Claude 4.8 Opus"),
            browser_profile_dir=profile_dir,
            headless=_env_bool("SYNTX_HEADLESS", False),
            page_limit=page_limit,
            page_idle_seconds=idle_seconds,
            request_timeout=timeout,
            selectors=SyntXSelectors.from_env(),
        )
