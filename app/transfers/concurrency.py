"""A counting semaphore whose limit can be changed while in use.

`asyncio.Semaphore` doesn't support resizing after construction, but the
"max concurrent downloads/uploads" setting needs to take effect immediately
even while transfers are already running -- rebuilding the semaphore on
every settings change would lose track of permits already checked out.
"""
from __future__ import annotations

import asyncio


class ResizableSemaphore:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self._in_use = 0
        self._cond = asyncio.Condition()

    def locked(self) -> bool:
        return self._in_use >= self.limit

    async def acquire(self) -> None:
        async with self._cond:
            while self._in_use >= self.limit:
                await self._cond.wait()
            self._in_use += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_use = max(0, self._in_use - 1)
            self._cond.notify()

    async def set_limit(self, new_limit: int) -> None:
        async with self._cond:
            self.limit = max(1, new_limit)
            self._cond.notify_all()

    async def __aenter__(self) -> "ResizableSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.release()
