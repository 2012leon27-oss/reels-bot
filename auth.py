"""Authentication helpers for browser and Telegram Mini App clients."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from aiohttp import web

from config import settings


@dataclass(frozen=True)
class Actor:
    id: str
    display_name: str


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def telegram_user_allowed(user_id: int) -> bool:
    allowed = settings.allowed_telegram_ids
    return user_id in allowed if allowed else not settings.production


def validate_telegram_init_data(
    init_data: str, bot_token: str, *, max_age_seconds: int = 86400
) -> dict:
    """Validate Telegram Web App initData and return its user object."""
    if not init_data or not bot_token:
        raise AuthError("Telegram authentication is unavailable.")

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise AuthError("Telegram signature is missing.")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(received_hash, calculated_hash):
        raise AuthError("Telegram signature is invalid.")

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError as exc:
        raise AuthError("Telegram authentication date is invalid.") from exc
    if auth_date <= 0 or abs(int(time.time()) - auth_date) > max_age_seconds:
        raise AuthError("Telegram authentication has expired.")

    try:
        user = json.loads(values.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise AuthError("Telegram user data is invalid.") from exc
    if not isinstance(user, dict) or not user.get("id"):
        raise AuthError("Telegram user is missing.")
    return user


def actor_from_request(request: web.Request) -> Actor:
    telegram_data = request.headers.get("X-Telegram-Init-Data", "")
    telegram_error: AuthError | None = None
    if telegram_data and settings.bot_token:
        try:
            user = validate_telegram_init_data(telegram_data, settings.bot_token)
            user_id = int(user["id"])
            if telegram_user_allowed(user_id):
                name = " ".join(
                    part
                    for part in (
                        str(user.get("first_name", "")),
                        str(user.get("last_name", "")),
                    )
                    if part
                ).strip()
                return Actor(f"telegram:{user_id}", name or "Telegram user")
            telegram_error = AuthError(
                "This Telegram account is not allowed to use the application.",
                status=403,
            )
        except AuthError as exc:
            telegram_error = exc

    provided_key = request.headers.get("X-App-Access-Key", "")
    if settings.app_access_key and hmac.compare_digest(
        provided_key, settings.app_access_key
    ):
        return Actor("direct:owner", "Owner")

    if not settings.app_access_key and not settings.bot_token:
        if settings.production:
            raise AuthError(
                "Set APP_ACCESS_KEY or BOT_TOKEN before using production.", status=503
            )
        return Actor("development:local", "Local user")

    if telegram_error:
        raise telegram_error
    raise AuthError(
        "Open the app from Telegram or enter the private access key.", status=401
    )
