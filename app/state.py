"""Shared in-process application state: DB handle, account store, active
logged-in MegaAPI sessions (keyed by email), and the in-memory transfer
registry used to push progress over the WebSocket.

A single process/single event loop local app doesn't need anything more
elaborate than module-level singletons for this.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import WebSocket

from app.core.mega_api import MegaAPI
from app.core.proxy_manager import SmartProxyManager
from app.storage.account_store import AccountStore
from app.storage.db import Database
from app.transfers.concurrency import ResizableSemaphore

db = Database()
account_store = AccountStore(db)
proxy_manager = SmartProxyManager()
smart_proxy_enabled = False


# Bounds how many downloads/uploads actually run at once (mirrors Java's
# max_running_trans, kept separate per direction like the original).
# Transfers beyond this sit at "queued" until a slot frees up. Resizable
# (not a plain asyncio.Semaphore) so the "max concurrent" setting can be
# changed live without disturbing transfers already holding a slot.
MAX_CONCURRENT_DOWNLOADS = 4
MAX_CONCURRENT_UPLOADS = 4
download_slots = ResizableSemaphore(MAX_CONCURRENT_DOWNLOADS)
upload_slots = ResizableSemaphore(MAX_CONCURRENT_UPLOADS)

# Default number of parallel chunk workers per transfer (the original's
# per-download/per-upload "slots" spinner default).
DEFAULT_DOWNLOAD_CHUNK_SLOTS = 4
DEFAULT_UPLOAD_CHUNK_SLOTS = 4
default_download_chunk_slots = DEFAULT_DOWNLOAD_CHUNK_SLOTS
default_upload_chunk_slots = DEFAULT_UPLOAD_CHUNK_SLOTS

# Where finished downloads land. Overridable via MEGABASTERD_DOWNLOAD_DIR
# (see routes_transfers.BASE_DIR) or the Downloads settings page.
BASE_DIR = Path(__file__).resolve().parent.parent
default_download_dir = Path(os.environ.get("MEGABASTERD_DOWNLOAD_DIR", str(BASE_DIR / "downloads")))

# If True (default), a download whose CBC-MAC doesn't match is reported as
# "mac_mismatch" instead of "done". The MAC is always computed either way --
# this only controls whether a mismatch is surfaced as an error.
verify_download_mac = True

# Custom MEGA API key (the "ak" query param) -- None uses MegaAPI's
# built-in default.
mega_api_key: str | None = None

# email -> logged-in MegaAPI instance (kept warm so uploads/dir listings
# don't have to re-login every request)
active_sessions: dict[str, MegaAPI] = {}

# transfer_id -> progress dict, shown in the UI and pushed over /ws
active_transfers: dict[str, dict] = {}

# transfer_id -> the asyncio.Task actually running the transfer, so it can be cancelled
transfer_tasks: dict[str, object] = {}

# transfer_id -> the Event passed to that transfer's Downloader/Uploader as
# pause_event. Cleared = paused (blocks new chunk work, lets in-flight
# work finish), set = running. Only present while the transfer is
# actually running (populated in _run_download/_run_upload, popped in
# their finally blocks) -- a transfer that's queued, starting, or already
# terminal has no entry here.
transfer_pause_events: dict[str, asyncio.Event] = {}

subscribers: list[WebSocket] = []

# Set during lifespan shutdown so in-flight downloads that get cancelled by
# the event loop tearing down don't overwrite their persisted "downloading"
# status (which is what lets them auto-resume on the next start) and don't
# delete their partial files. A hard SIGKILL skips this entirely, which is
# fine -- the DB row and partial file are already on disk.
shutting_down: bool = False

# Destination paths already claimed by an in-flight download but not yet
# necessarily written to disk -- prevents two concurrent downloads that
# resolve to the same filename from racing to pick the same "unique" path.
claimed_download_paths: set[str] = set()


def active_proxy_manager() -> SmartProxyManager | None:
    """The proxy manager if SmartProxy is enabled, else None -- so callers can
    pass the result straight through and get direct connections when it's off."""
    return proxy_manager if smart_proxy_enabled else None




async def broadcast(transfer_id: str) -> None:
    """Push a transfer's current progress dict to every connected WebSocket,
    dropping any subscriber whose send fails (a closed tab)."""
    payload = active_transfers[transfer_id]
    dead = []
    for ws in subscribers:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in subscribers:
            subscribers.remove(ws)
