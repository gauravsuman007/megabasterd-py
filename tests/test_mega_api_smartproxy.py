from unittest.mock import patch

import httpx
import pytest

from app.core.mega_api import MegaAPI
from app.core.proxy_manager import SmartProxyManager


@pytest.mark.asyncio
async def test_509_without_proxy_manager_is_not_retried():
    async def always_509(request: httpx.Request) -> httpx.Response:
        return httpx.Response(509)

    direct_client = httpx.AsyncClient(transport=httpx.MockTransport(always_509))
    api = MegaAPI(client=direct_client)  # no proxy_manager

    with pytest.raises(Exception):
        await api._raw_request([{"a": "uq"}])

    await api.aclose()


@pytest.mark.asyncio
async def test_509_routes_through_proxy_and_succeeds():
    call_log = []

    async def always_509(request: httpx.Request) -> httpx.Response:
        call_log.append("direct")
        return httpx.Response(509)

    async def succeeds(request: httpx.Request) -> httpx.Response:
        call_log.append("proxy")
        return httpx.Response(200, text="[{\"cstrg\": 1, \"mstrg\": 2}]")

    direct_client = httpx.AsyncClient(transport=httpx.MockTransport(always_509))
    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(succeeds))

    mgr = SmartProxyManager(random_select=False)

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("1.2.3.4:8080\n", fetch)

    api = MegaAPI(client=direct_client, proxy_manager=mgr)

    # Swap in our pre-built mock-transport client whenever the retry path
    # tries to construct an ad-hoc proxied httpx.AsyncClient, so the
    # "proxied" request is deterministic instead of hitting real network.
    with patch("app.core.mega_api.httpx.AsyncClient", return_value=proxy_client):
        result = await api._raw_request([{"a": "uq"}])

    assert result == [{"cstrg": 1, "mstrg": 2}]
    assert call_log[0] == "direct"
    assert "proxy" in call_log

    # The proxy worked on first use, so it was never blocked/banned -- only
    # a *second* 509 while already on a proxy would trigger that.
    assert mgr.proxy_count() == 1
    assert mgr.count_blocked() == 0

    await direct_client.aclose()
    await proxy_client.aclose()


@pytest.mark.asyncio
async def test_509_blocks_proxy_that_also_509s_and_tries_next_one():
    call_log = []

    async def always_509(request: httpx.Request) -> httpx.Response:
        call_log.append("direct")
        return httpx.Response(509)

    async def also_509(request: httpx.Request) -> httpx.Response:
        call_log.append("bad-proxy")
        return httpx.Response(509)

    async def succeeds(request: httpx.Request) -> httpx.Response:
        call_log.append("good-proxy")
        return httpx.Response(200, text='[{"cstrg": 1, "mstrg": 2}]')

    direct_client = httpx.AsyncClient(transport=httpx.MockTransport(always_509))
    bad_proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(also_509))
    good_proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(succeeds))

    mgr = SmartProxyManager(random_select=False)

    async def fetch(url):
        return ""

    # random_select=False -> pick_proxy iterates pool insertion order, so
    # the bad one is tried first, then (once excluded) the good one.
    await mgr.refresh_from_text("1.1.1.1:80\n2.2.2.2:80\n", fetch)

    api = MegaAPI(client=direct_client, proxy_manager=mgr)

    clients_in_order = [bad_proxy_client, good_proxy_client]
    with patch("app.core.mega_api.httpx.AsyncClient", side_effect=lambda **kw: clients_in_order.pop(0)):
        result = await api._raw_request([{"a": "uq"}])

    assert result == [{"cstrg": 1, "mstrg": 2}]
    assert call_log == ["direct", "bad-proxy", "good-proxy"]

    # The bad proxy got banned; the good one wasn't.
    assert mgr.count_blocked() == 1
    assert mgr.pick_proxy() == ("2.2.2.2:80", "http")

    await direct_client.aclose()
    await bad_proxy_client.aclose()
    await good_proxy_client.aclose()


@pytest.mark.asyncio
async def test_509_gives_up_when_proxy_pool_exhausted():
    async def always_509(request: httpx.Request) -> httpx.Response:
        return httpx.Response(509)

    direct_client = httpx.AsyncClient(transport=httpx.MockTransport(always_509))
    mgr = SmartProxyManager()  # empty pool -- pick_proxy always returns None

    api = MegaAPI(client=direct_client, proxy_manager=mgr)

    # Exhausting the (empty) pool still burns through MAX_RAW_REQUEST_RETRIES
    # exponential-backoff sleeps before giving up; skip the real waiting.
    with patch("app.core.mega_api.asyncio.sleep"):
        with pytest.raises(Exception):
            await api._raw_request([{"a": "uq"}])

    await direct_client.aclose()
