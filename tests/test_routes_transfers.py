import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app import state
from app.api.routes_transfers import (
    cancel_transfer,
    classify_links,
    clear_finished_transfers,
    create_download,
    list_transfers,
    pause_all_transfers,
    pause_transfer,
    resume_all_transfers,
    resume_transfer,
    stop_all_transfers,
)


@dataclass
class FakeMeta:
    name: str
    size: int


@dataclass
class FakeResult:
    mac_verified: bool


class FakeApi:
    def __init__(self, meta):
        self._meta = meta

    async def get_mega_file_metadata(self, link):
        return self._meta

    async def aclose(self):
        pass


def make_entry(id_, *, kind="download", status="downloading", link="l", speed=5.0):
    entry = {"id": id_, "kind": kind, "name": None, "status": status, "bytes_done": 0, "total": 100, "speed": speed}
    if kind == "download":
        entry["link"] = link
    return entry


@pytest.fixture
async def clean_transfer_state(tmp_path):
    from app.storage.db import Database

    original_db = state.db
    state.db = Database(tmp_path / "test.db")
    await state.db.connect()
    state.default_download_dir = tmp_path
    state.shutting_down = False
    yield
    await state.db.close()
    state.db = original_db
    state.active_transfers.clear()
    state.transfer_tasks.clear()
    state.transfer_pause_events.clear()
    state.claimed_download_paths.clear()


@pytest.mark.asyncio
async def test_pause_transfer_clears_event_and_zeroes_speed(clean_transfer_state):
    state.active_transfers["t1"] = make_entry("t1", status="downloading", speed=42.0)
    event = asyncio.Event()
    event.set()
    state.transfer_pause_events["t1"] = event

    await pause_transfer("t1")

    assert state.active_transfers["t1"]["status"] == "paused"
    assert state.active_transfers["t1"]["speed"] == 0.0
    assert not event.is_set()


@pytest.mark.asyncio
async def test_pause_transfer_rejects_non_running_transfer(clean_transfer_state):
    state.active_transfers["t1"] = make_entry("t1", status="starting")
    # no pause event registered yet -- mirrors a transfer that hasn't
    # acquired a concurrency slot and constructed its engine yet

    with pytest.raises(HTTPException) as exc_info:
        await pause_transfer("t1")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_resume_transfer_unpauses_running_transfer(clean_transfer_state):
    state.active_transfers["t1"] = make_entry("t1", kind="upload", status="paused")
    event = asyncio.Event()  # cleared -- currently paused
    state.transfer_pause_events["t1"] = event

    result = await resume_transfer("t1")

    assert result == {"ok": True, "action": "resumed"}
    assert event.is_set()
    assert state.active_transfers["t1"]["status"] == "uploading"


@pytest.mark.asyncio
async def test_resume_transfer_restarts_failed_download(clean_transfer_state):
    state.active_transfers["t1"] = make_entry("t1", status="error: boom", link="https://mega.nz/file/x#y")

    fake_api = FakeApi(FakeMeta(name="file.bin", size=100))
    fake_downloader = AsyncMock()
    fake_downloader.run.return_value = FakeResult(mac_verified=True)

    with (
        patch("app.api.routes_transfers.MegaAPI", return_value=fake_api),
        patch("app.api.routes_transfers.Downloader", return_value=fake_downloader),
    ):
        result = await resume_transfer("t1")
        assert result == {"ok": True, "action": "restarted"}
        assert state.active_transfers["t1"]["status"] == "starting"
        assert state.active_transfers["t1"]["bytes_done"] == 0

        task = state.transfer_tasks["t1"]
        await task

    assert state.active_transfers["t1"]["status"] == "done"


@pytest.mark.asyncio
async def test_resume_transfer_rejects_done_transfer(clean_transfer_state):
    state.active_transfers["t1"] = make_entry("t1", status="done")

    with pytest.raises(HTTPException) as exc_info:
        await resume_transfer("t1")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_resume_transfer_rejects_unknown_id(clean_transfer_state):
    with pytest.raises(HTTPException) as exc_info:
        await resume_transfer("nope")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_pause_all_only_affects_actively_running_transfers(clean_transfer_state):
    state.active_transfers["running"] = make_entry("running", status="downloading")
    state.active_transfers["queued"] = make_entry("queued", status="queued")
    state.active_transfers["done"] = make_entry("done", status="done")
    running_event = asyncio.Event()
    running_event.set()
    state.transfer_pause_events["running"] = running_event
    # "queued" and "done" deliberately have no pause event registered

    result = await pause_all_transfers()

    assert result["paused"] == ["running"]
    assert state.active_transfers["running"]["status"] == "paused"
    assert state.active_transfers["queued"]["status"] == "queued"
    assert state.active_transfers["done"]["status"] == "done"
    assert not running_event.is_set()


@pytest.mark.asyncio
async def test_resume_all_unpauses_and_restarts_failed_downloads(clean_transfer_state):
    state.active_transfers["paused"] = make_entry("paused", status="paused")
    state.active_transfers["failed"] = make_entry("failed", status="cancelled", link="https://mega.nz/file/x#y")
    state.active_transfers["done"] = make_entry("done", status="done")
    paused_event = asyncio.Event()  # cleared
    state.transfer_pause_events["paused"] = paused_event

    fake_api = FakeApi(FakeMeta(name="file.bin", size=100))
    fake_downloader = AsyncMock()
    fake_downloader.run.return_value = FakeResult(mac_verified=True)

    with (
        patch("app.api.routes_transfers.MegaAPI", return_value=fake_api),
        patch("app.api.routes_transfers.Downloader", return_value=fake_downloader),
    ):
        result = await resume_all_transfers()

        assert result["resumed"] == ["paused"]
        assert result["restarted"] == ["failed"]
        assert paused_event.is_set()
        assert state.active_transfers["done"]["status"] == "done"  # untouched

        await state.transfer_tasks["failed"]

    assert state.active_transfers["failed"]["status"] == "done"


@pytest.mark.asyncio
async def test_stop_all_cancels_every_tracked_task(clean_transfer_state):
    async def never_finishes():
        await asyncio.sleep(1000)

    task_a = asyncio.create_task(never_finishes())
    task_b = asyncio.create_task(never_finishes())
    state.transfer_tasks["a"] = task_a
    state.transfer_tasks["b"] = task_b

    result = await stop_all_transfers()

    assert set(result["stopped"]) == {"a", "b"}
    await asyncio.sleep(0)  # let cancellation propagate
    assert task_a.cancelled() or task_a.cancelling()
    assert task_b.cancelled() or task_b.cancelling()

    for t in (task_a, task_b):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_clear_finished_removes_only_terminal_transfers(clean_transfer_state):
    state.active_transfers["done"] = make_entry("done", status="done")
    state.active_transfers["error"] = make_entry("error", status="error: boom")
    state.active_transfers["cancelled"] = make_entry("cancelled", status="cancelled")
    state.active_transfers["mismatch"] = make_entry("mismatch", status="mac_mismatch")
    state.active_transfers["active"] = make_entry("active", status="downloading")
    state.active_transfers["queued"] = make_entry("queued", status="queued")

    result = await clear_finished_transfers()

    assert set(result["removed"]) == {"done", "error", "cancelled", "mismatch"}
    assert set(state.active_transfers.keys()) == {"active", "queued"}


@pytest.mark.asyncio
async def test_cancel_transfer_still_works_for_unknown_id(clean_transfer_state):
    with pytest.raises(HTTPException) as exc_info:
        await cancel_transfer("nope")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_download_persists_row_to_queue(clean_transfer_state):
    from app.api.routes_transfers import create_download

    with patch("app.api.routes_transfers._run_download", new=AsyncMock()):
        result = await create_download(link="https://mega.nz/file/abcdefgh#0123456789")

    rows = await state.db.get_download_queue()
    assert len(rows) == 1
    assert rows[0]["id"] == result["id"]
    assert rows[0]["link"] == "https://mega.nz/file/abcdefgh#0123456789"
    assert rows[0]["status"] == "starting"


@pytest.mark.asyncio
async def test_create_download_dedupes_active_link_without_forking(clean_transfer_state):
    """Regression: queuing the same file link twice (e.g. re-selecting a
    folder) must not create a second independent download. Before this, two
    ids/rows/partial files were created for one file, so after a restart the
    same episode showed up both 'cancelled' (a zombie) and 'downloading'."""
    link = "https://mega.nz/file/abcdefgh#0123456789"
    state.active_transfers["existing"] = make_entry("existing", status="downloading", link=link)
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task
    state.transfer_tasks["existing"] = done_task  # marks it as actively running

    with patch("app.api.routes_transfers._run_download", new=AsyncMock()) as run_mock:
        result = await create_download(link=link)

    assert result["id"] == "existing"
    assert result.get("duplicate") is True
    assert set(state.active_transfers.keys()) == {"existing"}  # no duplicate entry
    run_mock.assert_not_called()  # nothing re-launched for an already-active download


@pytest.mark.asyncio
async def test_create_download_retries_failed_link_in_place(clean_transfer_state):
    """Re-adding a link whose only entry is a failed/idle one (cancelled,
    errored, or restored idle after a restart) restarts THAT entry in place
    rather than forking a duplicate -- so the id/row/file stay singular."""
    link = "https://mega.nz/file/abcdefgh#0123456789"
    state.active_transfers["failed"] = make_entry("failed", status="cancelled", link=link)

    run_mock = AsyncMock()
    with patch("app.api.routes_transfers._run_download", run_mock):
        result = await create_download(link=link)

    assert result["id"] == "failed"
    assert set(state.active_transfers.keys()) == {"failed"}  # reused, not duplicated
    assert state.active_transfers["failed"]["status"] == "starting"  # restarted
    run_mock.assert_called_once()

    # Cancel the task _restart_download scheduled around the mocked coroutine.
    state.transfer_tasks.get("failed") and state.transfer_tasks["failed"].cancel()


@pytest.mark.asyncio
async def test_restore_download_queue_resumes_active_and_idles_failed(clean_transfer_state):
    from app.api.routes_transfers import restore_download_queue

    partial = state.default_download_dir / "movie.mkv"
    partial.write_bytes(b"x" * 500)  # 500 bytes already on disk

    await state.db.upsert_download_queue("d1", "https://mega.nz/file/a#b", "movie.mkv", str(partial), 1000, "downloading")
    await state.db.upsert_download_queue("d2", "https://mega.nz/file/c#d", "old.mkv", None, 2000, "error: boom")

    run_mock = AsyncMock()
    with patch("app.api.routes_transfers._run_download", run_mock):
        await restore_download_queue()

    # The mid-flight download is restored, its progress recovered from the
    # partial file, and it's relaunched with the exact resume path.
    assert state.active_transfers["d1"]["bytes_done"] == 500
    assert state.active_transfers["d1"]["status"] == "queued"
    run_mock.assert_called_once()
    assert run_mock.call_args.kwargs["resume_path"] == str(partial)
    assert run_mock.call_args.args[:2] == ("d1", "https://mega.nz/file/a#b")

    # The failed one is restored visible-but-idle -- no relaunch.
    assert state.active_transfers["d2"]["status"] == "error: boom"

    # Clean up the task create_task() scheduled around the mocked coroutine.
    state.transfer_tasks.get("d1") and state.transfer_tasks["d1"].cancel()


@pytest.mark.asyncio
async def test_list_transfers_returns_all_entries(clean_transfer_state):
    state.active_transfers["a"] = make_entry("a")
    state.active_transfers["b"] = make_entry("b", kind="upload")

    result = await list_transfers()

    assert {t["id"] for t in result} == {"a", "b"}


@pytest.mark.asyncio
async def test_create_download_rejects_folder_link(clean_transfer_state):
    """Regression test: pasting a folder link into the plain download
    field used to create a transfer that failed later with a cryptic
    MEGA -11 (Access denied), instead of being rejected up front with a
    clear "use the folder browser" message and no transfer ever created."""
    with pytest.raises(HTTPException) as exc_info:
        await create_download(link="https://mega.nz/folder/RAs0FQhJ#XrCLS3_t_9PFHxhrr0ocNw")
    assert exc_info.value.status_code == 400
    assert "folder link" in exc_info.value.detail
    assert state.active_transfers == {}  # nothing was created


@pytest.mark.asyncio
async def test_create_download_rejects_unparseable_link(clean_transfer_state):
    with pytest.raises(HTTPException) as exc_info:
        await create_download(link="not a mega link at all")
    assert exc_info.value.status_code == 400
    assert state.active_transfers == {}


@pytest.mark.asyncio
async def test_classify_links_sorts_file_folder_and_invalid():
    links = "\n".join(
        [
            "https://mega.nz/file/aaaaaaaa#bbbbbbbbbbbbbbbbbbbbbb",
            "https://mega.nz/folder/RAs0FQhJ#XrCLS3_t_9PFHxhrr0ocNw",
            "not a mega link at all",
            "",  # blank lines are dropped, not reported as invalid
            "   ",
        ]
    )

    result = await classify_links(links=links)

    assert result == {
        "results": [
            {"link": "https://mega.nz/file/aaaaaaaa#bbbbbbbbbbbbbbbbbbbbbb", "kind": "file"},
            {"link": "https://mega.nz/folder/RAs0FQhJ#XrCLS3_t_9PFHxhrr0ocNw", "kind": "folder"},
            {"link": "not a mega link at all", "kind": "invalid"},
        ]
    }


@pytest.mark.asyncio
async def test_classify_links_handles_empty_input():
    assert await classify_links(links="") == {"results": []}
    assert await classify_links(links="   \n  \n") == {"results": []}
