"""HTTP routes and background tasks driving the transfer queue.

Each transfer is a dict in `state.active_transfers` (the client polls/receives
these over the WebSocket) with an accompanying asyncio task in
`state.transfer_tasks` and, while running, a pause `Event` in
`state.transfer_pause_events`. Downloads are persisted to the DB so the queue
survives a restart (see `_persist_download` / `restore_download_queue`); uploads
are in-memory only. This module owns the create/list/cancel/pause/resume routes
plus the `_run_download`/`_run_upload` coroutines that actually drive the
Downloader/Uploader engines and translate their progress into queue updates.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException

from app import state
from app.core.link_parser import parse_mega_link
from app.core.mega_api import MegaAPI
from app.transfers.download import Downloader
from app.transfers.upload import Uploader

router = APIRouter(prefix="/api", tags=["transfers"])

TERMINAL_STATUS_KEYS = {"done", "cancelled", "mac_mismatch", "error"}
# "done" is a success, not a failure -- resume-all should restart the
# others, not re-run something that already finished correctly. "paused" is
# here too so a download restored from the DB after a restart (which has no
# live pause_event) can still be resumed via the same Resume button.
RESTARTABLE_DOWNLOAD_STATUS_KEYS = {"cancelled", "mac_mismatch", "error", "paused"}
ACTIVE_STATUS_KEYS = {"downloading", "uploading"}
# Statuses that mean "was actively in the queue" -- these get relaunched on
# startup so an interrupted download resumes itself; terminal/failed ones are
# restored visible-but-idle for the user to retry.
RESUMABLE_ON_STARTUP_KEYS = {"starting", "queued", "downloading"}

# transfer_id -> (last_sample_monotonic_ts, last_sample_bytes_done), used
# only to derive entry["speed"] in progress_cb -- kept out of the entry
# dict itself so this bookkeeping doesn't get serialized to the client.
_speed_trackers: dict[str, tuple[float, float]] = {}
_SPEED_SMOOTHING = 0.3  # low-pass factor so speed doesn't jump wildly between chunk callbacks
_MIN_SPEED_SAMPLE_INTERVAL = 0.2  # seconds


def _status_key(status: str) -> str:
    """The bare status word without any detail suffix (e.g. ``error: ...`` ->
    ``error``), for comparing against the status-set constants above."""
    return status.split(":")[0].strip()


async def _persist_download(entry: dict) -> None:
    """Snapshot a download's queue-relevant fields to the DB so the queue
    survives a container restart. Called on status transitions and once
    metadata/dest are resolved -- deliberately NOT on byte progress, since
    bytes_done is recovered from the partial file's size on resume, so the
    high-frequency progress callback never hits SQLite."""
    if entry.get("kind") != "download" or state.shutting_down:
        return
    await state.db.upsert_download_queue(
        id=entry["id"],
        link=entry["link"],
        name=entry.get("name"),
        path=entry.get("path"),
        total=entry.get("total", 0),
        status=entry["status"],
    )


def _update_speed(transfer_id: str, entry: dict, done: int) -> None:
    """Update `entry["speed"]` (bytes/sec) from the latest byte count, using a
    minimum sample interval and low-pass smoothing so it doesn't jitter between
    chunk callbacks. The first call just seeds the baseline."""
    now = time.monotonic()
    tracker = _speed_trackers.get(transfer_id)
    if tracker is None:
        # First sample: seed the baseline so the next callback has a real
        # (ts, bytes) pair to measure against. Skipping this seeding (the old
        # bug) left dt permanently ~0, so speed never left 0.
        _speed_trackers[transfer_id] = (now, done)
        return
    last_ts, last_bytes = tracker
    dt = now - last_ts
    if dt < _MIN_SPEED_SAMPLE_INTERVAL:
        return
    instantaneous = max(0.0, (done - last_bytes) / dt)
    prev_speed = entry.get("speed", 0.0)
    entry["speed"] = _SPEED_SMOOTHING * instantaneous + (1 - _SPEED_SMOOTHING) * prev_speed
    _speed_trackers[transfer_id] = (now, done)


def _finish_speed_tracking(entry: dict, transfer_id: str) -> None:
    """Zero the displayed speed and drop the tracker when a transfer ends."""
    entry["speed"] = 0.0
    _speed_trackers.pop(transfer_id, None)


def _claim_unique_path(directory: Path, filename: str) -> Path:
    """Pick a destination path that doesn't collide with an existing file
    or another in-flight download's already-claimed (but maybe not yet
    created) destination, appending " (1)", " (2)", ... as needed."""
    stem, suffix = os.path.splitext(filename)
    candidate = directory / filename
    n = 1
    while str(candidate) in state.claimed_download_paths or candidate.exists():
        candidate = directory / f"{stem} ({n}){suffix}"
        n += 1
    state.claimed_download_paths.add(str(candidate))
    return candidate


@router.get("/transfers")
async def list_transfers():
    """Every current transfer's progress dict (the initial UI snapshot; live
    updates then arrive over the WebSocket)."""
    return list(state.active_transfers.values())


@router.delete("/transfers/{transfer_id}")
async def cancel_transfer(transfer_id: str):
    """Remove a transfer at the user's request. Cancels its live task (whose
    cleanup deletes the partial file and DB row), or tears it down directly if
    it already finished. A completed download's file is kept. 404 if unknown."""
    entry = state.active_transfers.get(transfer_id)
    if entry is None:
        raise HTTPException(404, "No such transfer")

    # Mark as a genuine user removal so the running task's cleanup deletes the
    # partial file and drops the DB row (vs. a Stop-all/shutdown cancel, which
    # keeps both for resume).
    entry["_user_removed"] = True

    task = state.transfer_tasks.get(transfer_id)
    if task is not None:
        task.cancel()  # its finally block does the file + DB cleanup
        return {"ok": True}

    # No live task (already finished/failed, or restored from a past run):
    # tear it down here directly. A completed download's file is kept -- only
    # an incomplete download's partial file is garbage worth deleting.
    path = entry.get("path")
    if path and _status_key(entry["status"]) != "done" and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    await state.db.delete_download_queue(transfer_id)
    state.active_transfers.pop(transfer_id, None)
    return {"ok": True}


@router.post("/transfers/{transfer_id}/pause")
async def pause_transfer(transfer_id: str):
    """Pause a running transfer (clears its pause event so new chunk work
    blocks). 400 if it isn't currently running."""
    entry = state.active_transfers.get(transfer_id)
    event = state.transfer_pause_events.get(transfer_id)
    if entry is None or event is None or _status_key(entry["status"]) not in ACTIVE_STATUS_KEYS:
        raise HTTPException(400, "Transfer is not currently running")
    event.clear()
    entry["status"] = "paused"
    entry["speed"] = 0.0
    await _persist_download(entry)
    await state.broadcast(transfer_id)
    return {"ok": True}


@router.post("/transfers/{transfer_id}/resume")
async def resume_transfer(transfer_id: str):
    """Resume a transfer: un-pause a live one, or restart a failed/idle download
    from its on-disk prefix (one with no live task, e.g. after a restart). 404
    if unknown, 400 if it's neither paused nor restartable."""
    entry = state.active_transfers.get(transfer_id)
    if entry is None:
        raise HTTPException(404, "No such transfer")

    event = state.transfer_pause_events.get(transfer_id)
    if event is not None:
        event.set()
        entry["status"] = "downloading" if entry["kind"] == "download" else "uploading"
        await _persist_download(entry)
        await state.broadcast(transfer_id)
        return {"ok": True, "action": "resumed"}

    if entry["kind"] == "download" and _status_key(entry["status"]) in RESTARTABLE_DOWNLOAD_STATUS_KEYS:
        await _restart_download(transfer_id)
        return {"ok": True, "action": "restarted"}

    raise HTTPException(400, "Transfer is not paused and can't be restarted")


@router.post("/transfers/pause-all")
async def pause_all_transfers():
    """Pause every currently-running transfer. Returns the ids paused."""
    paused = []
    for transfer_id, entry in list(state.active_transfers.items()):
        event = state.transfer_pause_events.get(transfer_id)
        if event is not None and _status_key(entry["status"]) in ACTIVE_STATUS_KEYS:
            event.clear()
            entry["status"] = "paused"
            entry["speed"] = 0.0
            await _persist_download(entry)
            paused.append(transfer_id)
    for transfer_id in paused:
        await state.broadcast(transfer_id)
    return {"ok": True, "paused": paused}


@router.post("/transfers/resume-all")
async def resume_all_transfers():
    """Un-pauses anything paused, and -- per the "resume all should also
    try to start failed/broken downloads" request -- relaunches any
    download that ended in a failure state (error, cancelled, or a MAC
    mismatch) from scratch, reusing its original link. Uploads aren't
    auto-restarted: there's no persisted resume point for them, and
    restarting one means re-reading the same local file, so it's left as
    an explicit per-row action instead of an automatic bulk one."""
    resumed = []
    restarted = []
    for transfer_id, entry in list(state.active_transfers.items()):
        event = state.transfer_pause_events.get(transfer_id)
        if event is not None:
            event.set()
            entry["status"] = "downloading" if entry["kind"] == "download" else "uploading"
            await _persist_download(entry)
            resumed.append(transfer_id)
            continue
        if entry["kind"] == "download" and _status_key(entry["status"]) in RESTARTABLE_DOWNLOAD_STATUS_KEYS:
            await _restart_download(transfer_id)
            restarted.append(transfer_id)

    for transfer_id in resumed:
        await state.broadcast(transfer_id)
    return {"ok": True, "resumed": resumed, "restarted": restarted}


@router.post("/transfers/stop-all")
async def stop_all_transfers():
    """Cancel every live transfer task. Partial files/DB rows are kept (this is
    a stop, not a removal), so they can be resumed. Returns the ids stopped."""
    stopped = list(state.transfer_tasks.keys())
    for task in state.transfer_tasks.values():
        task.cancel()
    return {"ok": True, "stopped": stopped}


@router.post("/transfers/clear-finished")
async def clear_finished_transfers():
    """Remove every transfer in a terminal state (done/cancelled/error/mac
    mismatch) from the queue and DB, deleting the partial files of the failed
    ones (a completed download's file is kept). Returns the ids removed."""
    removed = [tid for tid, entry in state.active_transfers.items() if _status_key(entry["status"]) in TERMINAL_STATUS_KEYS]
    for tid in removed:
        entry = state.active_transfers.pop(tid, None)
        if entry is None:
            continue
        # Drop the persisted queue row so it doesn't reappear on restart.
        await state.db.delete_download_queue(tid)
        # An incomplete (failed/cancelled) download's partial file is garbage;
        # a completed one's file is the actual download and is kept.
        path = entry.get("path")
        if path and _status_key(entry["status"]) != "done" and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    return {"ok": True, "removed": removed}


@router.post("/links/classify")
async def classify_links(links: str = Form(...)):
    """Pure parsing, no network calls -- lets the unified multiline
    download field sort a pasted batch into file links (queued straight
    away), folder links (opened in the file-picker popup), and anything
    that isn't a recognizable MEGA link at all, without duplicating the
    link-format regexes client-side."""
    results = []
    for line in links.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            kind = parse_mega_link(line).kind
        except ValueError:
            kind = "invalid"
        results.append({"link": line, "kind": kind})
    return {"results": results}


async def _restart_download(transfer_id: str) -> None:
    """Relaunch a failed/idle download under its existing id, resuming from any
    whole-chunk prefix already on disk (a MAC-mismatch file is discarded first,
    since its bytes are corrupt)."""
    entry = state.active_transfers[transfer_id]
    path = entry.get("path")

    # A MAC mismatch means the fully-downloaded bytes are corrupt, so resuming
    # would just re-verify the same bad data -- discard and re-fetch cleanly.
    if _status_key(entry["status"]) == "mac_mismatch" and path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass
        path = None

    # Otherwise resume from whatever whole-chunk prefix is already on disk.
    resume_path = path if path and os.path.exists(path) else None
    entry.update(
        status="starting",
        bytes_done=os.path.getsize(resume_path) if resume_path else 0,
        speed=0.0,
    )
    await _persist_download(entry)
    await state.broadcast(transfer_id)
    state.transfer_tasks[transfer_id] = asyncio.create_task(
        _run_download(transfer_id, entry["link"], resume_path=resume_path)
    )


def _find_download_by_link(link: str) -> dict | None:
    """The queue is keyed by transfer id, but a MEGA file link is the real
    identity of a download. Used to keep the same link from being enqueued
    twice (which used to create two independent downloads, two DB rows and
    two " (1)" files -- so after a restart you'd see the same episode both
    'cancelled' and 'downloading')."""
    for entry in state.active_transfers.values():
        if entry.get("kind") == "download" and entry.get("link") == link:
            return entry
    return None


@router.post("/downloads")
async def create_download(link: str = Form(...)):
    """Queue a MEGA *file* link for download and kick off its task. De-duped by
    link: an existing active/queued/done copy is returned untouched, a
    failed/idle one is retried in place. 400 for an invalid or folder link."""
    try:
        parsed = parse_mega_link(link)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if parsed.kind == "folder":
        raise HTTPException(400, 'This is a folder link, not a file link -- use "Browse folder link" to pick individual files to download.')

    # De-dupe by link: never create a second download of a file that's
    # already in the queue. If the existing one is actively downloading,
    # queued or already done, this is a no-op that just returns it; if it's
    # a failed/idle one (e.g. cancelled by Stop-all, or restored idle after
    # a restart), retry it in place -- reusing its id -- instead of forking
    # a duplicate.
    existing = _find_download_by_link(link)
    if existing is not None:
        existing_id = existing["id"]
        status = _status_key(existing["status"])
        has_live_task = existing_id in state.transfer_tasks
        if not has_live_task and status in {"cancelled", "mac_mismatch", "error", "paused"}:
            await _restart_download(existing_id)
        return {"id": existing_id, "duplicate": True}

    transfer_id = str(uuid.uuid4())
    entry = {
        "id": transfer_id,
        "kind": "download",
        "link": link,
        "name": None,
        "path": None,
        "status": "starting",
        "bytes_done": 0,
        "total": 0,
        "speed": 0.0,
    }
    state.active_transfers[transfer_id] = entry
    await _persist_download(entry)
    state.transfer_tasks[transfer_id] = asyncio.create_task(_run_download(transfer_id, link))
    return {"id": transfer_id}


async def _run_download(transfer_id: str, link: str, resume_path: str | None = None) -> None:
    """Background task that runs one download end to end: resolve metadata, claim
    a destination path, wait for a concurrency slot, then drive the Downloader,
    updating the queue entry's status/progress and broadcasting throughout.
    `resume_path` continues an existing file instead of claiming a new name.
    Sets the terminal status (done/mac_mismatch/cancelled/error) and handles the
    partial-file/DB cleanup in its finally block."""
    entry = state.active_transfers[transfer_id]
    # No proxy for this MegaAPI instance: it's only used for metadata /
    # download-URL lookups (here and reused internally by Downloader.run),
    # not for fetching actual file bytes. Only the Downloader below --
    # which does the real chunk-by-chunk byte transfer -- gets a proxy_manager.
    api = MegaAPI(api_key=state.mega_api_key, proxy_manager=None)
    # On a restart-resume we already know the exact destination file (from the
    # persisted queue) and want to continue it, not claim a fresh " (1)" name.
    dest: Path | None = Path(resume_path) if resume_path else None
    resuming = resume_path is not None
    pause_event = asyncio.Event()
    pause_event.set()
    state.transfer_pause_events[transfer_id] = pause_event
    try:
        meta = await api.get_mega_file_metadata(link)
        entry.update(name=meta.name, total=meta.size)

        if dest is None:
            download_dir = state.default_download_dir
            download_dir.mkdir(parents=True, exist_ok=True)
            dest = _claim_unique_path(download_dir, meta.name)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            state.claimed_download_paths.add(str(dest))
        entry["path"] = str(dest)
        await _persist_download(entry)  # now carries name/path/total
        await state.broadcast(transfer_id)

        if state.download_slots.locked():
            entry["status"] = "queued"
            await _persist_download(entry)
            await state.broadcast(transfer_id)

        async with state.download_slots:
            entry["status"] = "downloading"
            await _persist_download(entry)
            await state.broadcast(transfer_id)

            def progress_cb(done: int, total: int) -> None:
                entry["bytes_done"] = done
                _update_speed(transfer_id, entry, done)
                asyncio.create_task(state.broadcast(transfer_id))

            downloader = Downloader(
                api,
                link,
                str(dest),
                slots=state.default_download_chunk_slots,
                progress_cb=progress_cb,
                proxy_manager=state.active_proxy_manager(),
                pause_event=pause_event,
                resume=resuming,
            )
            result = await downloader.run()
        if result.mac_verified or not state.verify_download_mac:
            entry["status"] = "done"
        else:
            entry["status"] = "mac_mismatch"
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
        # Only an explicit user removal deletes the partial file. A cancel
        # from shutdown or Stop-all keeps it so the download can resume.
        if entry.get("_user_removed") and dest is not None and dest.exists():
            dest.unlink()
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        entry["status"] = f"error: {exc}"
    finally:
        await api.aclose()
        if dest is not None:
            state.claimed_download_paths.discard(str(dest))
        state.transfer_tasks.pop(transfer_id, None)
        state.transfer_pause_events.pop(transfer_id, None)
        _finish_speed_tracking(entry, transfer_id)
        # A completed or user-removed download leaves the persistent queue;
        # everything else stays so it can resume/retry after a restart.
        # (Skipped entirely during shutdown -- see _persist_download.)
        if not state.shutting_down:
            if _status_key(entry["status"]) == "done" or entry.get("_user_removed"):
                await state.db.delete_download_queue(transfer_id)
            else:
                await _persist_download(entry)
        await state.broadcast(transfer_id)


async def restore_download_queue() -> None:
    """Rebuild the in-memory download queue from the DB at startup so it
    survives container restarts. Downloads that were mid-flight (or waiting to
    start) are relaunched -- resuming from their partial file -- while ones
    that had failed/been stopped are restored visible-but-idle for the user to
    retry. Called once from the app lifespan."""
    rows = await state.db.get_download_queue()
    for row in rows:
        transfer_id = row["id"]
        status = row["status"] or "queued"
        path = row["path"]
        # Recover progress from the partial file rather than the DB.
        bytes_done = os.path.getsize(path) if path and os.path.exists(path) else 0

        entry = {
            "id": transfer_id,
            "kind": "download",
            "link": row["link"],
            "name": row["name"],
            "path": path,
            "status": status,
            "bytes_done": bytes_done,
            "total": row["total"] or 0,
            "speed": 0.0,
        }
        state.active_transfers[transfer_id] = entry

        if _status_key(status) in RESUMABLE_ON_STARTUP_KEYS:
            entry["status"] = "queued"
            state.transfer_tasks[transfer_id] = asyncio.create_task(
                _run_download(transfer_id, row["link"], resume_path=path)
            )
        # else: cancelled/error/mac_mismatch/paused -> left idle & retriable.


@router.post("/uploads")
async def create_upload(email: str = Form(...), file_path: str = Form(...), parent_node: str | None = Form(None)):
    """Queue a local file for upload to `email`'s account (into `parent_node`, or
    the account root) and start its task. 400 if there's no active session for
    that account or the file doesn't exist."""
    api = state.active_sessions.get(email)
    if api is None:
        raise HTTPException(400, f"No active session for {email} -- log in first")
    if not os.path.isfile(file_path):
        raise HTTPException(400, f"No such file: {file_path}")

    transfer_id = str(uuid.uuid4())
    state.active_transfers[transfer_id] = {
        "id": transfer_id,
        "kind": "upload",
        "name": os.path.basename(file_path),
        "status": "starting",
        "bytes_done": 0,
        "total": os.path.getsize(file_path),
        "speed": 0.0,
    }
    state.transfer_tasks[transfer_id] = asyncio.create_task(
        _run_upload(transfer_id, api, file_path, parent_node or api.root_id)
    )
    return {"id": transfer_id}


async def _run_upload(transfer_id: str, api: MegaAPI, file_path: str, parent_node: str) -> None:
    """Background task that runs one upload: wait for a slot, drive the Uploader,
    and update the queue entry's status/progress. Sets the terminal status
    (done/cancelled/error) in its finally block. Uploads aren't persisted, so
    there's no DB/partial-file cleanup here (unlike `_run_download`)."""
    entry = state.active_transfers[transfer_id]
    pause_event = asyncio.Event()
    pause_event.set()
    state.transfer_pause_events[transfer_id] = pause_event
    try:
        if state.upload_slots.locked():
            entry["status"] = "queued"
            await state.broadcast(transfer_id)

        async with state.upload_slots:
            entry["status"] = "uploading"
            await state.broadcast(transfer_id)

            def progress_cb(done: int, total: int) -> None:
                entry["bytes_done"] = done
                _update_speed(transfer_id, entry, done)
                asyncio.create_task(state.broadcast(transfer_id))

            uploader = Uploader(
                api,
                file_path,
                parent_node,
                slots=state.default_upload_chunk_slots,
                progress_cb=progress_cb,
                proxy_manager=state.active_proxy_manager(),
                pause_event=pause_event,
            )
            result = await uploader.run()
        entry["status"] = "done"
        entry["node_handle"] = result.node_handle
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        entry["status"] = f"error: {exc}"
    finally:
        state.transfer_tasks.pop(transfer_id, None)
        state.transfer_pause_events.pop(transfer_id, None)
        _finish_speed_tracking(entry, transfer_id)
        await state.broadcast(transfer_id)
