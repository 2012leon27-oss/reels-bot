"""Per-conversation locks and a global SyntX request queue."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


class DialogueQueue:
    """Serialize work per dialogue key and track global queue depth."""

    def __init__(self, max_dialogue_locks: int = 500) -> None:
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._max_locks = max_dialogue_locks
        self._global_depth = 0
        self._depth_lock = asyncio.Lock()

    @property
    def queue_depth(self) -> int:
        return self._global_depth

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key in self._locks:
            self._locks.move_to_end(key)
            return self._locks[key]
        lock = asyncio.Lock()
        self._locks[key] = lock
        while len(self._locks) > self._max_locks:
            oldest_key, oldest_lock = self._locks.popitem(last=False)
            if oldest_lock.locked():
                # Keep locked dialogues; put back and stop shrinking.
                self._locks[oldest_key] = oldest_lock
                self._locks.move_to_end(oldest_key, last=False)
                break
        return lock

    async def run_for_dialogue(
        self,
        key: str,
        operation: Callable[[], Awaitable[T]],
        *,
        enqueued_at: float | None = None,
        max_age_seconds: float = 120.0,
        stale_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> T | None:
        async with self._depth_lock:
            self._global_depth += 1
        try:
            lock = self._get_lock(key)
            async with lock:
                if enqueued_at is not None and (time.monotonic() - enqueued_at) > max_age_seconds:
                    if stale_check is None or not await stale_check():
                        logger.info("Dropping stale queued job for dialogue %s", key)
                        return None
                return await operation()
        finally:
            async with self._depth_lock:
                self._global_depth = max(0, self._global_depth - 1)


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "operation",
) -> T:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            logger.warning("%s attempt %s/%s failed: %s", label, attempt, attempts, type(exc).__name__)
            if attempt < attempts:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc
