from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Per-source throttle: caps in-flight requests to `concurrency` AND spaces
    successive requests at least `min_interval_s` apart.

    Use as an async context manager around each HTTP call:

        async with limiter:
            resp = await client.get(url)

    The interval gate is enforced under a lock so concurrent workers serialize on
    the "next allowed start" timestamp; the semaphore bounds parallelism. Both are
    no-ops at their permissive extremes (large concurrency / 0 interval).
    """

    def __init__(self, concurrency: int, min_interval_s: float = 0.0):
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._min_interval = max(0.0, min_interval_s)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        await self._sem.acquire()
        if self._min_interval:
            async with self._lock:
                now = time.monotonic()
                wait = self._next_allowed - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                self._next_allowed = now + self._min_interval

    def release(self) -> None:
        self._sem.release()

    async def __aenter__(self) -> "RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()
