"""Small health server used by hosting platforms such as Render."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web

HealthCheck = Callable[[], Awaitable[bool]]


async def start_webserver(port: int, health_check: HealthCheck) -> web.AppRunner:
    async def root(_: web.Request) -> web.Response:
        return web.json_response(
            {"service": "telegram-business-assistant", "status": "running"}
        )

    async def health(_: web.Request) -> web.Response:
        healthy = await health_check()
        return web.json_response(
            {"status": "ok" if healthy else "unhealthy"},
            status=200 if healthy else 503,
        )

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
