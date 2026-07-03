from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app import state
from app.api import routes_accounts, routes_settings
from app.storage.account_store import AccountStore
from app.storage.db import Database


@pytest.fixture
async def client(tmp_path):
    # Fresh DB per test (routes read/write via app.state.db, a singleton
    # shared across the whole app) and a snapshot/restore of the mutable
    # globals these routes touch, so tests can't leak into each other.
    original_db = state.db
    original_account_store = state.account_store
    snapshot = dict(
        smart_proxy_enabled=state.smart_proxy_enabled,
        default_download_chunk_slots=state.default_download_chunk_slots,
        default_upload_chunk_slots=state.default_upload_chunk_slots,
        default_download_dir=state.default_download_dir,
        verify_download_mac=state.verify_download_mac,
        mega_api_key=state.mega_api_key,
    )
    download_limit = state.download_slots.limit
    upload_limit = state.upload_slots.limit
    original_verified_pool_provider = state.proxy_manager.verified_pool_provider

    state.db = Database(tmp_path / "settings_test.db")
    await state.db.connect()
    state.account_store = AccountStore(state.db)

    app = FastAPI()
    app.include_router(routes_settings.router)
    app.include_router(routes_accounts.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await state.db.close()
    state.db = original_db
    state.account_store = original_account_store
    state.proxy_manager.verified_pool_provider = original_verified_pool_provider
    for key, value in snapshot.items():
        setattr(state, key, value)
    await state.download_slots.set_limit(download_limit)
    await state.upload_slots.set_limit(upload_limit)


@pytest.mark.asyncio
async def test_download_settings_roundtrip(client):
    resp = await client.post(
        "/api/settings/downloads",
        data={"default_dir": "/tmp/my-downloads", "max_concurrent": "7", "default_slots": "3", "verify_mac": "false"},
    )
    assert resp.status_code == 200

    resp = await client.get("/api/settings/downloads")
    data = resp.json()
    assert data["default_dir"] == "/tmp/my-downloads"
    assert data["max_concurrent"] == 7
    assert data["default_slots"] == 3
    assert data["verify_mac"] is False

    assert state.download_slots.limit == 7
    assert state.default_download_chunk_slots == 3
    assert state.verify_download_mac is False


@pytest.mark.asyncio
async def test_download_settings_clamp_out_of_range_values(client):
    resp = await client.post(
        "/api/settings/downloads",
        data={"default_dir": "/tmp/x", "max_concurrent": "9999", "default_slots": "0", "verify_mac": "true"},
    )
    assert resp.status_code == 200
    data = (await client.get("/api/settings/downloads")).json()
    assert data["max_concurrent"] == 100  # clamped to max
    assert data["default_slots"] == 1  # clamped to min


@pytest.mark.asyncio
async def test_upload_settings_roundtrip(client):
    resp = await client.post("/api/settings/uploads", data={"max_concurrent": "9", "default_slots": "6"})
    assert resp.status_code == 200

    data = (await client.get("/api/settings/uploads")).json()
    assert data["max_concurrent"] == 9
    assert data["default_slots"] == 6
    assert state.upload_slots.limit == 9
    assert state.default_upload_chunk_slots == 6


@pytest.mark.asyncio
async def test_advanced_settings_roundtrip(client):
    resp = await client.post("/api/settings/advanced", data={"mega_api_key": "myCustomKey123"})
    assert resp.status_code == 200

    data = (await client.get("/api/settings/advanced")).json()
    assert data["mega_api_key"] == "myCustomKey123"
    assert state.mega_api_key == "myCustomKey123"

    # Blank clears it back to None (use built-in default).
    await client.post("/api/settings/advanced", data={"mega_api_key": ""})
    assert state.mega_api_key is None




@pytest.mark.asyncio
async def test_accounts_endpoint_reports_encrypted_flag(client):
    data = (await client.get("/api/accounts")).json()
    assert data["locked"] is False
    assert data["encrypted"] is False

    await client.post("/api/accounts/master-password", data={"password": "s3cr3t"})
    data = (await client.get("/api/accounts")).json()
    assert data["encrypted"] is True
