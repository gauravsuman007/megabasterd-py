"""FastAPI app entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import state
from app.api import routes_accounts, routes_folder, routes_settings, routes_transfers, ws
from app.api.routes_settings import _http_fetch
from app.streaming import video_proxy

BASE_DIR = Path(__file__).resolve().parent.parent


async def _load_smartproxy_settings() -> None:
    """Restore SmartProxy config from the DB into `state` on startup, refreshing
    the live pool from the saved list if SmartProxy is enabled."""
    mgr = state.proxy_manager
    state.smart_proxy_enabled = (await state.db.get_setting("use_smart_proxy")) == "yes"
    mgr.ban_time = int(await state.db.get_setting("smartproxy_ban_time") or mgr.ban_time)
    mgr.proxy_timeout = int(await state.db.get_setting("smartproxy_timeout") or mgr.proxy_timeout)
    mgr.force_smart_proxy = (await state.db.get_setting("force_smart_proxy")) == "yes"
    mgr.random_select = (await state.db.get_setting("random_proxy")) != "no"

    if state.smart_proxy_enabled:
        custom_proxy_list = await state.db.get_setting("custom_proxy_list") or ""
        await mgr.refresh_from_text(custom_proxy_list, _http_fetch)




async def _load_transfer_settings() -> None:
    """Restore transfer-related settings (concurrency limits, default slots, MAC
    verification, download dir, custom API key) from the DB into `state`."""
    max_downloads = await state.db.get_setting("max_concurrent_downloads")
    if max_downloads:
        await state.download_slots.set_limit(int(max_downloads))
    max_uploads = await state.db.get_setting("max_concurrent_uploads")
    if max_uploads:
        await state.upload_slots.set_limit(int(max_uploads))

    state.default_download_chunk_slots = int(await state.db.get_setting("default_download_slots") or state.DEFAULT_DOWNLOAD_CHUNK_SLOTS)
    state.default_upload_chunk_slots = int(await state.db.get_setting("default_upload_slots") or state.DEFAULT_UPLOAD_CHUNK_SLOTS)
    state.verify_download_mac = (await state.db.get_setting("verify_download_mac")) != "no"

    download_dir = await state.db.get_setting("default_download_dir")
    if download_dir:
        state.default_download_dir = Path(download_dir)

    state.mega_api_key = await state.db.get_setting("mega_api_key") or None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup/shutdown. On startup: open the DB, load all settings, and
    restore the persisted download queue (last, so relaunched downloads see the
    fully-loaded settings). On shutdown: flag the shutdown, stop background
    tasks, and close all sessions and the DB."""
    await state.db.connect()
    await _load_smartproxy_settings()
    await _load_transfer_settings()
    # Restore the persisted download queue last, so relaunched downloads see
    # the fully-loaded settings (download dir, slot limits, api key).
    await routes_transfers.restore_download_queue()
    try:
        yield
    finally:
        # Signal in-flight downloads not to clobber their persisted state or
        # partial files as the event loop cancels them on the way down.
        state.shutting_down = True
        for api in state.active_sessions.values():
            await api.aclose()
        await state.db.close()


# Version of the original Java MegaBasterd this port tracks (MainPanel.VERSION).
APP_VERSION = "8.57"

app = FastAPI(title="MegaBasterd-Py", version=APP_VERSION, lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
templates.env.globals["app_version"] = APP_VERSION
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")

app.include_router(routes_accounts.router)
app.include_router(routes_transfers.router)
app.include_router(routes_settings.router)
app.include_router(routes_folder.router)
app.include_router(video_proxy.router)
app.include_router(ws.router)


def _asset_version() -> str:
    """Mtime-based cache-busting token for static assets, so editing
    app.js/style.css during development doesn't get masked by the
    browser's HTTP cache silently serving a stale copy."""
    static_dir = BASE_DIR / "web" / "static"
    newest = max((f.stat().st_mtime for f in static_dir.rglob("*") if f.is_file()), default=0)
    return str(int(newest))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the main page (downloads/uploads/transfers)."""
    return templates.TemplateResponse(request, "index.html", {"asset_version": _asset_version()})


@app.get("/watch", response_class=HTMLResponse)
async def watch(request: Request):
    """Render the video-streaming page."""
    return templates.TemplateResponse(request, "watch.html", {"asset_version": _asset_version()})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Render the settings page."""
    return templates.TemplateResponse(request, "settings.html", {"asset_version": _asset_version()})
