"""HTTP routes backing the Settings page: SmartProxy config, the background
proxy diagnostics toggle and free-proxy fetch (private edition only), and the
Downloads/Uploads/Advanced setting groups. Each GET returns the current values;
each POST validates/clamps, applies to `state`, and persists to the DB."""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import APIRouter, Form

from app import state

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _http_fetch(url: str) -> str:
    """GET a remote proxy-list URL and return its body (used to resolve the
    `#https://...` source lines in a SmartProxy list)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


@router.get("/smartproxy")
async def get_smartproxy_settings():
    """Current SmartProxy settings and live pool counts."""
    mgr = state.proxy_manager
    return {
        "enabled": state.smart_proxy_enabled,
        "custom_proxy_list": await state.db.get_setting("custom_proxy_list") or "",
        "ban_time": mgr.ban_time,
        "proxy_timeout": mgr.proxy_timeout,
        "force_smart_proxy": mgr.force_smart_proxy,
        "random_select": mgr.random_select,
        "proxy_count": mgr.proxy_count(),
        "blocked_count": mgr.count_blocked(),
    }


@router.post("/smartproxy")
async def update_smartproxy_settings(
    enabled: bool = Form(False),
    custom_proxy_list: str = Form(""),
    ban_time: int = Form(300),
    proxy_timeout: int = Form(45),
    force_smart_proxy: bool = Form(False),
    random_select: bool = Form(True),
):
    """Save SmartProxy settings (clamping ban time/timeout), persist them, and
    refresh the live pool from the given list. Returns the refresh result."""
    state.smart_proxy_enabled = enabled
    mgr = state.proxy_manager
    mgr.ban_time = max(0, min(ban_time, 3600))
    mgr.proxy_timeout = max(3, min(proxy_timeout, 120))
    mgr.force_smart_proxy = force_smart_proxy
    mgr.random_select = random_select

    await state.db.set_setting("use_smart_proxy", "yes" if enabled else "no")
    await state.db.set_setting("custom_proxy_list", custom_proxy_list)
    await state.db.set_setting("smartproxy_ban_time", str(mgr.ban_time))
    await state.db.set_setting("smartproxy_timeout", str(mgr.proxy_timeout))
    await state.db.set_setting("force_smart_proxy", "yes" if force_smart_proxy else "no")
    await state.db.set_setting("random_proxy", "yes" if random_select else "no")


    result = await mgr.refresh_from_text(custom_proxy_list, _http_fetch)
    return {"ok": True, "entries": result.entries, "urls_ok": result.urls_ok, "urls_failed": result.urls_failed}


@router.post("/smartproxy/refresh")
async def refresh_smartproxy():
    """Re-parse the saved proxy list and rebuild the live pool (re-fetching any
    remote source URLs), without changing any saved settings."""
    custom_proxy_list = await state.db.get_setting("custom_proxy_list") or ""
    result = await state.proxy_manager.refresh_from_text(custom_proxy_list, _http_fetch)
    return {"ok": True, "entries": result.entries, "urls_ok": result.urls_ok, "urls_failed": result.urls_failed}




@router.get("/downloads")
async def get_download_settings():
    """Current download settings (dir, concurrency, per-transfer slots, MAC check)."""
    return {
        "default_dir": str(state.default_download_dir),
        "max_concurrent": state.download_slots.limit,
        "default_slots": state.default_download_chunk_slots,
        "verify_mac": state.verify_download_mac,
    }


@router.post("/downloads")
async def update_download_settings(
    default_dir: str = Form(...),
    max_concurrent: int = Form(4),
    default_slots: int = Form(4),
    verify_mac: bool = Form(True),
):
    """Apply and persist download settings, clamping concurrency to 1-100 and
    per-transfer slots to 1-20. The concurrency change takes effect live."""
    max_concurrent = max(1, min(max_concurrent, 100))
    default_slots = max(1, min(default_slots, 20))

    state.default_download_dir = Path(default_dir)
    await state.download_slots.set_limit(max_concurrent)
    state.default_download_chunk_slots = default_slots
    state.verify_download_mac = verify_mac

    await state.db.set_setting("default_download_dir", default_dir)
    await state.db.set_setting("max_concurrent_downloads", str(max_concurrent))
    await state.db.set_setting("default_download_slots", str(default_slots))
    await state.db.set_setting("verify_download_mac", "yes" if verify_mac else "no")
    return {"ok": True}


@router.get("/uploads")
async def get_upload_settings():
    """Current upload concurrency and per-transfer slot settings."""
    return {
        "max_concurrent": state.upload_slots.limit,
        "default_slots": state.default_upload_chunk_slots,
    }


@router.post("/uploads")
async def update_upload_settings(max_concurrent: int = Form(4), default_slots: int = Form(4)):
    """Apply and persist upload settings (same 1-100 / 1-20 clamps as downloads)."""
    max_concurrent = max(1, min(max_concurrent, 100))
    default_slots = max(1, min(default_slots, 20))

    await state.upload_slots.set_limit(max_concurrent)
    state.default_upload_chunk_slots = default_slots

    await state.db.set_setting("max_concurrent_uploads", str(max_concurrent))
    await state.db.set_setting("default_upload_slots", str(default_slots))
    return {"ok": True}


@router.get("/advanced")
async def get_advanced_settings():
    """The custom MEGA API key, or "" if using the built-in default."""
    return {"mega_api_key": state.mega_api_key or ""}


@router.post("/advanced")
async def update_advanced_settings(mega_api_key: str = Form("")):
    """Set or clear (blank) the custom MEGA API key."""
    state.mega_api_key = mega_api_key or None
    await state.db.set_setting("mega_api_key", mega_api_key)
    return {"ok": True}
