"""aiohttp application for the Thought Architect web and Mini App UI."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from collections import defaultdict, deque
from typing import Any

from aiohttp import web

from auth import AuthError, actor_from_request
from config import settings
from database import (
    close_db,
    create_note,
    database_health,
    delete_note,
    get_note,
    get_public_note,
    init_db,
    list_notes,
    set_note_sharing,
)
from grok_client import (
    AIConfigurationError,
    AIResponseError,
    MODES,
    render_markdown,
    structure_thoughts,
    transcribe_audio,
)
from note_export import export_note, notes_dir_path

logger = logging.getLogger(__name__)
ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_ROOT = os.path.join(ROOT, "static")


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        events = self._events[key]
        while events and events[0] <= now - self.window_seconds:
            events.popleft()
        if len(events) >= self.limit:
            raise web.HTTPTooManyRequests(
                text='{"error":"Слишком много запросов. Попробуйте немного позже."}',
                content_type="application/json",
            )
        events.append(now)


@web.middleware
async def error_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    request["request_started"] = time.monotonic()
    try:
        response = await handler(request)
    except AuthError as exc:
        response = web.json_response({"error": str(exc)}, status=exc.status)
    except AIConfigurationError as exc:
        logger.error("AI configuration error: %s", exc)
        response = web.json_response(
            {"error": "Сервис ИИ ещё не настроен. Добавьте ключ API."}, status=503
        )
    except AIResponseError as exc:
        logger.warning("Invalid AI response: %s", exc)
        response = web.json_response(
            {"error": "Не удалось корректно обработать ответ ИИ. Повторите запрос."},
            status=502,
        )
    except web.HTTPException as exc:
        response = exc
    except Exception:
        logger.exception("Unhandled request error for %s", request.path)
        response = web.json_response(
            {"error": "Внутренняя ошибка. Попробуйте ещё раз."}, status=500
        )

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), geolocation=(), microphone=(self)"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' https://telegram.org; "
        "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "media-src 'self' blob:; frame-ancestors 'self' "
        "https://telegram.org https://*.telegram.org",
    )
    return response


def _base_url(request: web.Request) -> str:
    if settings.app_base_url:
        return settings.app_base_url
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0]
    scheme = forwarded_proto.strip() or request.scheme
    return f"{scheme}://{request.host}".rstrip("/")


def _with_links(note: dict[str, Any], request: web.Request) -> dict[str, Any]:
    result = dict(note)
    result["share_url"] = f"{_base_url(request)}/s/{result['share_id']}"
    result.pop("owner_id", None)
    result["share_enabled"] = bool(result.get("share_enabled"))
    result["share_source"] = bool(result.get("share_source"))
    if result.get("file_name"):
        result["file_url"] = f"{_base_url(request)}/files/{result['file_name']}"
        result["index_url"] = f"{_base_url(request)}/files/index.md"
    return result


def _export_note_files(note: dict[str, Any]) -> dict[str, Any]:
    paths = export_note(note)
    note.update(paths)
    return note


async def index(request: web.Request) -> web.FileResponse:
    response = web.FileResponse(os.path.join(STATIC_ROOT, "index.html"))
    response.headers["Cache-Control"] = "no-cache"
    return response


async def health(request: web.Request) -> web.Response:
    database = await database_health()
    configured = bool(settings.ai_api_key)
    status = 200 if database["status"] == "ok" and configured else 503
    return web.json_response(
        {
            "status": "ok" if status == 200 else "degraded",
            "database": database,
            "ai_configured": bool(settings.ai_api_key),
            "ai_model": settings.ai_model,
            "ai_provider": "cursor-sdk",
            "transcription_configured": bool(settings.transcription_api_key),
        },
        status=status,
    )


async def structure(request: web.Request) -> web.Response:
    actor = actor_from_request(request)
    request.app["structure_limiter"].check(actor.id)
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(
            text='{"error":"Ожидался JSON-запрос."}',
            content_type="application/json",
        ) from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(
            text='{"error":"Некорректный формат запроса."}',
            content_type="application/json",
        )

    source_text = str(body.get("text") or "").strip()
    mode = str(body.get("mode") or "auto")
    if not source_text:
        raise web.HTTPBadRequest(
            text='{"error":"Добавьте текст или голосовую запись."}',
            content_type="application/json",
        )
    if len(source_text) > settings.max_text_chars:
        raise web.HTTPRequestEntityTooLarge(
            max_size=settings.max_text_chars, actual_size=len(source_text)
        )
    if mode not in MODES:
        mode = "auto"

    structured = await structure_thoughts(source_text, mode)
    markdown = render_markdown(structured, source_text)
    note = await create_note(
        owner_id=actor.id,
        source_text=source_text,
        structured=structured,
        structured_markdown=markdown,
        mode=mode,
    )
    note = _export_note_files(note)
    return web.json_response(_with_links(note, request), status=201)


async def transcribe(request: web.Request) -> web.Response:
    actor = actor_from_request(request)
    request.app["transcription_limiter"].check(actor.id)
    if not request.content_type.startswith("multipart/"):
        raise web.HTTPBadRequest(
            text='{"error":"Отправьте аудиофайл в multipart/form-data."}',
            content_type="application/json",
        )

    reader = await request.multipart()
    field = await reader.next()
    while field is not None and field.name != "audio":
        field = await reader.next()
    if field is None:
        raise web.HTTPBadRequest(
            text='{"error":"Аудиофайл не найден."}', content_type="application/json"
        )

    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", field.filename or "recording.webm")
    content_type = field.headers.get("Content-Type", "audio/webm")
    if not (
        content_type.startswith("audio/")
        or content_type in {"video/webm", "application/octet-stream"}
    ):
        raise web.HTTPUnsupportedMediaType(
            text='{"error":"Этот формат аудио не поддерживается."}',
            content_type="application/json",
        )

    total = 0
    with tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as audio_file:
        while chunk := await field.read_chunk(size=64 * 1024):
            total += len(chunk)
            if total > settings.max_audio_bytes:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=settings.max_audio_bytes, actual_size=total
                )
            audio_file.write(chunk)
        if total == 0:
            raise web.HTTPBadRequest(
                text='{"error":"Аудиофайл пуст."}',
                content_type="application/json",
            )
        audio_file.seek(0)
        text = await transcribe_audio(
            audio_file,
            filename=filename,
            content_type=content_type,
            language=request.query.get("language"),
        )
    return web.json_response({"text": text})


async def history(request: web.Request) -> web.Response:
    actor = actor_from_request(request)
    try:
        limit = int(request.query.get("limit", "30"))
    except ValueError:
        limit = 30
    notes = await list_notes(actor.id, limit)
    return web.json_response({"notes": [_with_links(note, request) for note in notes]})


async def private_note(request: web.Request) -> web.Response:
    actor = actor_from_request(request)
    note = await get_note(request.match_info["note_id"], actor.id)
    if not note:
        raise web.HTTPNotFound(
            text='{"error":"Заметка не найдена."}', content_type="application/json"
        )
    return web.json_response(_with_links(note, request))


async def public_note(request: web.Request) -> web.Response:
    note = await get_public_note(request.match_info["share_id"])
    if not note:
        raise web.HTTPNotFound(
            text='{"error":"Заметка не найдена или закрыта."}',
            content_type="application/json",
        )
    result = _with_links(note, request)
    result.pop("id", None)
    result.pop("structured_markdown", None)
    if not result.get("share_source"):
        result.pop("source_text", None)
    return web.json_response(result)


async def update_sharing(request: web.Request) -> web.Response:
    actor = actor_from_request(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(
            text='{"error":"Ожидался JSON-запрос."}',
            content_type="application/json",
        ) from exc
    if not isinstance(body, dict):
        body = {}
    note = await set_note_sharing(
        request.match_info["note_id"],
        actor.id,
        enabled=bool(body.get("enabled", True)),
        include_source=bool(body.get("include_source", False)),
    )
    if not note:
        raise web.HTTPNotFound(
            text='{"error":"Заметка не найдена."}', content_type="application/json"
        )
    return web.json_response(_with_links(note, request))


async def remove_note(request: web.Request) -> web.Response:
    actor = actor_from_request(request)
    removed = await delete_note(request.match_info["note_id"], actor.id)
    if not removed:
        raise web.HTTPNotFound(
            text='{"error":"Заметка не найдена."}', content_type="application/json"
        )
    return web.Response(status=204)


async def on_startup(app: web.Application) -> None:
    if settings.production:
        missing = []
        if not settings.ai_api_key:
            missing.append("CURSOR_API_KEY (secrets/api.key)")
        if not settings.database_url:
            missing.append("DATABASE_URL")
        if not settings.app_access_key and not settings.bot_token:
            missing.append("APP_ACCESS_KEY or BOT_TOKEN")
        if settings.bot_token and not settings.allowed_telegram_ids:
            missing.append("TELEGRAM_ALLOWED_USER_IDS or ADMIN_ID")
        if missing:
            raise RuntimeError(
                "Production configuration is incomplete: " + ", ".join(missing)
            )
    await init_db()


async def on_cleanup(app: web.Application) -> None:
    await close_db()


def create_app() -> web.Application:
    app = web.Application(
        middlewares=[error_middleware],
        client_max_size=settings.max_audio_bytes + 1024 * 1024,
    )
    app["structure_limiter"] = RateLimiter(limit=60, window_seconds=3600)
    app["transcription_limiter"] = RateLimiter(limit=60, window_seconds=3600)
    app.router.add_get("/", index)
    app.router.add_get("/s/{share_id}", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/structure", structure)
    app.router.add_post("/api/transcribe", transcribe)
    app.router.add_get("/api/notes", history)
    app.router.add_get("/api/notes/{note_id}", private_note)
    app.router.add_post("/api/notes/{note_id}/share", update_sharing)
    app.router.add_delete("/api/notes/{note_id}", remove_note)
    app.router.add_get("/api/public/{share_id}", public_note)
    app.router.add_static("/files/", str(notes_dir_path()), show_index=True)
    app.router.add_static("/static/", STATIC_ROOT, append_version=True)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
