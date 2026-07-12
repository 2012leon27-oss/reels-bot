"""Local FastAPI bridge that drives SyntX chat via Playwright."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from pydantic import BaseModel, Field

from integrations.syntx.config import CHAT_UUID_PATTERN, SyntXSettings
from integrations.syntx.errors import ModelMismatchError
from integrations.syntx.model_guard import ModelGuard
from integrations.syntx.page_manager import PageManager

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    chat_url: str | None = None
    model: str = "Claude 4.8 Opus"
    strict_model: bool = True


class CreateChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "Claude 4.8 Opus"
    strict_model: bool = True


class ChatResponse(BaseModel):
    answer: str
    resolved_chat_url: str
    model_ok: bool


class HealthResponse(BaseModel):
    ok: bool
    session_ok: bool
    model_available: bool
    queue_depth: int
    pages_count: int
    last_error: str | None = None


@dataclass
class BridgeRuntime:
    settings: SyntXSettings
    page_manager: PageManager
    model_guard: ModelGuard
    playwright: Playwright | None = None
    context: BrowserContext | None = None
    chat_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    create_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue_depth: int = 0
    last_error: str | None = None
    session_ok: bool = False
    model_available: bool = False

    async def startup(self) -> None:
        self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.browser_profile_dir),
            headless=self.settings.headless,
        )
        await self.refresh_health()

    async def shutdown(self) -> None:
        await self.page_manager.close_all()
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None

    def increment_queue(self) -> None:
        self.queue_depth += 1

    def decrement_queue(self) -> None:
        self.queue_depth = max(0, self.queue_depth - 1)

    def record_error(self, message: str) -> None:
        self.last_error = message
        logger.error(message)

    async def refresh_health(self) -> None:
        if self.context is None:
            self.session_ok = False
            self.model_available = False
            return
        page = await self.context.new_page()
        try:
            await page.goto(
                self.settings.new_chat_full_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            self.session_ok = not await is_login_page(page, self.settings.selectors)
            if self.session_ok:
                displayed = await self.model_guard.read_displayed_model(page)
                self.model_available = self.model_guard.model_matches(
                    displayed,
                    self.settings.model_name,
                )
            else:
                self.model_available = False
        except Exception as exc:
            self.session_ok = False
            self.model_available = False
            self.record_error(f"Health probe failed: {exc}")
        finally:
            await page.close()


def extract_chat_uuid(url: str) -> str | None:
    match = CHAT_UUID_PATTERN.search(url)
    return match.group(0) if match else None


def resolved_chat_url_from_page(page: Page) -> str:
    return page.url


async def is_login_page(page: Page, selectors: Any) -> bool:
    url = page.url.lower()
    if "login" in url or "sign-in" in url or "signin" in url:
        return True
    login_locator = page.locator(selectors.login_indicator)
    try:
        return await login_locator.first.is_visible()
    except Exception:
        return False


async def wait_for_assistant_reply(
    page: Page,
    *,
    selectors: Any,
    previous_count: int,
    timeout_ms: int,
) -> str:
    locator = page.locator(selectors.assistant_message)
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000

    while asyncio.get_running_loop().time() < deadline:
        count = await locator.count()
        if count > previous_count:
            text = (await locator.nth(count - 1).inner_text()).strip()
            if text:
                return text
        await asyncio.sleep(0.5)

    raise TimeoutError("Timed out waiting for assistant reply")


async def send_prompt(
    page: Page,
    *,
    prompt: str,
    selectors: Any,
    timeout_seconds: float,
) -> str:
    assistant_locator = page.locator(selectors.assistant_message)
    previous_count = await assistant_locator.count()

    textarea = page.locator(selectors.textarea).first
    await textarea.wait_for(state="visible", timeout=30_000)
    await textarea.fill(prompt)

    send_button = page.locator(selectors.send_button).first
    await send_button.wait_for(state="visible", timeout=15_000)
    await send_button.click()

    return await wait_for_assistant_reply(
        page,
        selectors=selectors,
        previous_count=previous_count,
        timeout_ms=int(timeout_seconds * 1000),
    )


async def wait_for_chat_uuid(page: Page, *, timeout_seconds: float) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        chat_uuid = extract_chat_uuid(page.url)
        if chat_uuid:
            return page.url
        await asyncio.sleep(0.5)
    raise TimeoutError("Chat URL did not receive a UUID in time")


async def close_evicted_pages(evicted: list[tuple[str, Any]]) -> None:
    for _url, entry in evicted:
        try:
            await entry.page.close()
        except Exception as exc:
            logger.warning("Failed to close evicted page: %s", exc)


async def get_or_open_chat_page(
    runtime: BridgeRuntime,
    chat_url: str | None,
) -> tuple[Page, str]:
    if runtime.context is None:
        raise RuntimeError("Browser context is not initialized")

    await runtime.page_manager.close_idle()

    if chat_url:
        cached = runtime.page_manager.touch(chat_url)
        if cached is not None:
            return cached.page, chat_url

        page = await runtime.context.new_page()
        await page.goto(chat_url, wait_until="domcontentloaded", timeout=60_000)
        resolved = resolved_chat_url_from_page(page)
        evicted = runtime.page_manager.register(resolved, page)
        await close_evicted_pages(evicted)
        return page, resolved

    page = await runtime.context.new_page()
    await page.goto(
        runtime.settings.new_chat_full_url,
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    resolved = resolved_chat_url_from_page(page)
    evicted = runtime.page_manager.register(resolved, page)
    await close_evicted_pages(evicted)
    return page, resolved


async def assert_session_ok(page: Page, runtime: BridgeRuntime) -> None:
    if await is_login_page(page, runtime.settings.selectors):
        runtime.session_ok = False
        raise HTTPException(
            status_code=401,
            detail="SyntX session expired; log in via the persistent browser profile",
        )
    runtime.session_ok = True


async def perform_chat(
    runtime: BridgeRuntime,
    *,
    prompt: str,
    chat_url: str | None,
    model: str,
    strict_model: bool,
    require_uuid: bool,
) -> ChatResponse:
    page, resolved_url = await get_or_open_chat_page(runtime, chat_url)
    await assert_session_ok(page, runtime)

    model_ok = await runtime.model_guard.ensure_model(
        page,
        model,
        strict_model=strict_model,
    )
    if strict_model and not model_ok:
        raise HTTPException(
            status_code=409,
            detail=f"Model {model!r} could not be confirmed in the UI",
        )

    answer = await send_prompt(
        page,
        prompt=prompt,
        selectors=runtime.settings.selectors,
        timeout_seconds=runtime.settings.request_timeout,
    )

    if require_uuid:
        resolved_url = await wait_for_chat_uuid(
            page,
            timeout_seconds=runtime.settings.request_timeout,
        )
        evicted = runtime.page_manager.register(resolved_url, page)
        await close_evicted_pages(evicted)
    else:
        resolved_url = resolved_chat_url_from_page(page)
        runtime.page_manager.touch(resolved_url)

    return ChatResponse(
        answer=answer,
        resolved_chat_url=resolved_url,
        model_ok=model_ok,
    )


def create_app(settings: SyntXSettings | None = None) -> FastAPI:
    resolved_settings = settings or SyntXSettings.from_env()
    runtime = BridgeRuntime(
        settings=resolved_settings,
        page_manager=PageManager(
            page_limit=resolved_settings.page_limit,
            idle_seconds=resolved_settings.page_idle_seconds,
        ),
        model_guard=ModelGuard(resolved_settings.selectors),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(title="SyntX Bridge", lifespan=lifespan)

    @app.post("/syntx_chat", response_model=ChatResponse)
    async def syntx_chat(request: ChatRequest) -> ChatResponse:
        runtime.increment_queue()
        try:
            async with runtime.chat_lock:
                try:
                    return await perform_chat(
                        runtime,
                        prompt=request.prompt,
                        chat_url=request.chat_url,
                        model=request.model,
                        strict_model=request.strict_model,
                        require_uuid=False,
                    )
                except ModelMismatchError as exc:
                    runtime.record_error(str(exc))
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                except TimeoutError as exc:
                    runtime.record_error(str(exc))
                    raise HTTPException(status_code=504, detail=str(exc)) from exc
                except HTTPException:
                    raise
                except Exception as exc:
                    runtime.record_error(str(exc))
                    raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            runtime.decrement_queue()

    @app.post("/syntx_create_chat", response_model=ChatResponse)
    async def syntx_create_chat(request: CreateChatRequest) -> ChatResponse:
        runtime.increment_queue()
        try:
            async with runtime.create_lock:
                async with runtime.chat_lock:
                    if runtime.context is None:
                        raise HTTPException(status_code=503, detail="Bridge not ready")

                    page = await runtime.context.new_page()
                    try:
                        await page.goto(
                            runtime.settings.new_chat_full_url,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        await assert_session_ok(page, runtime)

                        model_ok = await runtime.model_guard.ensure_model(
                            page,
                            request.model,
                            strict_model=request.strict_model,
                        )
                        if request.strict_model and not model_ok:
                            displayed = await runtime.model_guard.read_displayed_model(page)
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"Model {request.model!r} could not be confirmed; "
                                    f"UI shows {displayed!r}"
                                ),
                            )

                        answer = await send_prompt(
                            page,
                            prompt=request.prompt,
                            selectors=runtime.settings.selectors,
                            timeout_seconds=runtime.settings.request_timeout,
                        )
                        resolved_url = await wait_for_chat_uuid(
                            page,
                            timeout_seconds=runtime.settings.request_timeout,
                        )
                        evicted = runtime.page_manager.register(resolved_url, page)
                        await close_evicted_pages(evicted)

                        return ChatResponse(
                            answer=answer,
                            resolved_chat_url=resolved_url,
                            model_ok=model_ok,
                        )
                    except ModelMismatchError as exc:
                        await page.close()
                        runtime.record_error(str(exc))
                        raise HTTPException(status_code=409, detail=str(exc)) from exc
                    except TimeoutError as exc:
                        await page.close()
                        runtime.record_error(str(exc))
                        raise HTTPException(status_code=504, detail=str(exc)) from exc
                    except HTTPException:
                        await page.close()
                        raise
                    except Exception as exc:
                        await page.close()
                        runtime.record_error(str(exc))
                        raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            runtime.decrement_queue()

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        try:
            await runtime.refresh_health()
        except Exception as exc:
            runtime.record_error(str(exc))

        ok = runtime.context is not None and runtime.session_ok
        return HealthResponse(
            ok=ok,
            session_ok=runtime.session_ok,
            model_available=runtime.model_available,
            queue_depth=runtime.queue_depth,
            pages_count=len(runtime.page_manager),
            last_error=runtime.last_error,
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = SyntXSettings.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if settings.bridge_host != "127.0.0.1":
        logger.warning(
            "SYNTX_BRIDGE_HOST=%r ignored; bridge binds only to 127.0.0.1",
            settings.bridge_host,
        )
    uvicorn.run(
        "integrations.syntx.bridge:app",
        host="127.0.0.1",
        port=settings.bridge_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
