"""Small health server used by hosting platforms and local checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

HealthCheck = Callable[[], Awaitable[bool | dict[str, Any]]]


async def start_webserver(port: int, health_check: HealthCheck) -> web.AppRunner:
    async def root(_: web.Request) -> web.Response:
        return web.json_response(
            {"service": "telegram-business-neuroagent", "status": "running"}
        )

    async def health(_: web.Request) -> web.Response:
        result = await health_check()
        if isinstance(result, dict):
            ok = bool(result.get("ok", True))
            payload = result
        else:
            ok = bool(result)
            payload = {"ok": ok, "status": "ok" if ok else "unhealthy"}
        return web.json_response(payload, status=200 if ok else 503)

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
