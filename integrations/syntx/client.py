"""Async HTTP client for the local SyntX Playwright bridge."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from integrations.syntx.config import SyntXSettings
from integrations.syntx.errors import ModelMismatchError, SyntXBridgeError

logger = logging.getLogger(__name__)


def _model_error_message(payload: dict[str, Any]) -> str | None:
    detail = payload.get("detail")
    if isinstance(detail, str) and "model" in detail.lower():
        return detail
    error = payload.get("error")
    if isinstance(error, str) and "model" in error.lower():
        return error
    return None


class SyntXClient:
    def __init__(
        self,
        settings: SyntXSettings | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.settings = settings or SyntXSettings.from_env()
        bridge_root = base_url or self.settings.bridge_url.rsplit("/", 1)[0]
        self._base_url = bridge_root.rstrip("/")
        self._timeout = timeout if timeout is not None else self.settings.request_timeout
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
        )

    async def chat(
        self,
        prompt: str,
        *,
        chat_url: str | None = None,
        model: str | None = None,
        strict_model: bool = True,
    ) -> dict[str, str]:
        payload = {
            "prompt": prompt,
            "chat_url": chat_url,
            "model": model or self.settings.model_name,
            "strict_model": strict_model,
        }
        data = await self._post_json("/syntx_chat", payload)
        self._ensure_model_ok(data, strict_model)
        return {
            "answer": str(data["answer"]),
            "resolved_chat_url": str(data["resolved_chat_url"]),
        }

    async def create_chat(
        self,
        prompt: str,
        *,
        model: str | None = None,
        strict_model: bool = True,
    ) -> dict[str, str]:
        payload = {
            "prompt": prompt,
            "model": model or self.settings.model_name,
            "strict_model": strict_model,
        }
        data = await self._post_json("/syntx_create_chat", payload)
        self._ensure_model_ok(data, strict_model)
        return {
            "answer": str(data["answer"]),
            "resolved_chat_url": str(data["resolved_chat_url"]),
        }

    async def health(self) -> dict[str, Any]:
        response = await self._client.get("/health")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise SyntXBridgeError("Health endpoint returned invalid JSON")
        return payload

    async def close(self) -> None:
        await self._client.aclose()

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise SyntXBridgeError(f"SyntX bridge request failed: {exc}") from exc

        body: Any
        try:
            body = response.json()
        except ValueError as exc:
            raise SyntXBridgeError(
                f"SyntX bridge returned non-JSON response (HTTP {response.status_code})"
            ) from exc

        if not isinstance(body, dict):
            raise SyntXBridgeError("SyntX bridge returned invalid JSON object")

        if response.status_code >= 400:
            model_message = _model_error_message(body)
            if model_message or body.get("model_ok") is False:
                raise ModelMismatchError(
                    model_message or f"Model mismatch (HTTP {response.status_code})"
                )
            detail = body.get("detail") or body.get("error") or response.text
            raise SyntXBridgeError(f"SyntX bridge error (HTTP {response.status_code}): {detail}")

        return body

    def _ensure_model_ok(self, payload: dict[str, Any], strict_model: bool) -> None:
        if not strict_model:
            return
        if payload.get("model_ok") is False:
            raise ModelMismatchError("Bridge reported model_ok=false")
        model_message = _model_error_message(payload)
        if model_message:
            raise ModelMismatchError(model_message)
