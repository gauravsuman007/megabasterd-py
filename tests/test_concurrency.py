import asyncio

import pytest

from app.transfers.concurrency import ResizableSemaphore


@pytest.mark.asyncio
async def test_basic_acquire_release_respects_limit():
    sem = ResizableSemaphore(2)
    assert not sem.locked()
    await sem.acquire()
    await sem.acquire()
    assert sem.locked()
    await sem.release()
    assert not sem.locked()
    await sem.release()


@pytest.mark.asyncio
async def test_third_waiter_blocks_until_release():
    sem = ResizableSemaphore(2)
    await sem.acquire()
    await sem.acquire()

    events = []

    async def waiter():
        await sem.acquire()
        events.append("acquired")

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert events == []  # still blocked, both permits held

    await sem.release()
    await asyncio.wait_for(task, timeout=1.0)
    assert events == ["acquired"]


@pytest.mark.asyncio
async def test_async_context_manager():
    sem = ResizableSemaphore(1)
    async with sem:
        assert sem.locked()
    assert not sem.locked()


@pytest.mark.asyncio
async def test_increasing_limit_wakes_waiters_immediately():
    sem = ResizableSemaphore(1)
    await sem.acquire()  # only permit taken

    events = []

    async def waiter():
        await sem.acquire()
        events.append("acquired")

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert events == []

    await sem.set_limit(2)  # raise the cap without anyone releasing
    await asyncio.wait_for(task, timeout=1.0)
    assert events == ["acquired"]


@pytest.mark.asyncio
async def test_decreasing_limit_does_not_evict_current_holders():
    sem = ResizableSemaphore(3)
    await sem.acquire()
    await sem.acquire()
    await sem.set_limit(1)  # already 2 in use, over the new limit
    assert sem.locked()

    events = []

    async def waiter():
        await sem.acquire()
        events.append("acquired")

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert events == []  # still over limit, must wait

    await sem.release()
    await sem.release()
    await asyncio.wait_for(task, timeout=1.0)
    assert events == ["acquired"]
