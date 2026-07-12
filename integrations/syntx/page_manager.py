"""LRU cache for Playwright chat pages keyed by resolved chat URL."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol


class PlaywrightPage(Protocol):
    async def close(self) -> None: ...


@dataclass
class CachedPage:
    page: Any
    last_used: float


class PageManager:
    """Tracks open chat tabs and evicts least-recently-used pages."""

    def __init__(self, *, page_limit: int, idle_seconds: float) -> None:
        if page_limit < 1:
            raise ValueError("page_limit must be at least 1")
        self.page_limit = page_limit
        self.idle_seconds = idle_seconds
        self._pages: OrderedDict[str, CachedPage] = OrderedDict()

    def __len__(self) -> int:
        return len(self._pages)

    def touch(self, chat_url: str) -> CachedPage | None:
        entry = self._pages.get(chat_url)
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        self._pages.move_to_end(chat_url)
        return entry

    def register(self, chat_url: str, page: Any) -> list[tuple[str, CachedPage]]:
        self._pages[chat_url] = CachedPage(page=page, last_used=time.monotonic())
        self._pages.move_to_end(chat_url)
        return self._evict_over_limit()

    def remove(self, chat_url: str) -> CachedPage | None:
        return self._pages.pop(chat_url, None)

    async def close_entry(self, chat_url: str) -> None:
        entry = self.remove(chat_url)
        if entry is not None:
            await entry.page.close()

    async def close_idle(self) -> list[str]:
        now = time.monotonic()
        expired = [
            url
            for url, entry in self._pages.items()
            if now - entry.last_used >= self.idle_seconds
        ]
        for url in expired:
            await self.close_entry(url)
        return expired

    async def close_all(self) -> None:
        urls = list(self._pages.keys())
        for url in urls:
            await self.close_entry(url)

    def _evict_over_limit(self) -> list[tuple[str, CachedPage]]:
        evicted: list[tuple[str, CachedPage]] = []
        while len(self._pages) > self.page_limit:
            evicted.append(self._pages.popitem(last=False))
        return evicted
