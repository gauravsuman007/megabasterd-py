from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from app import state
from app.api.routes_transfers import _run_download


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


@pytest.fixture
async def clean_transfer_state(tmp_path):
    from app.storage.db import Database

    original_db = state.db
    state.db = Database(tmp_path / "test.db")
    await state.db.connect()
    state.default_download_dir = tmp_path
    state.shutting_down = False
    original_verify = state.verify_download_mac
    original_smart_proxy_enabled = state.smart_proxy_enabled
    yield
    await state.db.close()
    state.db = original_db
    state.verify_download_mac = original_verify
    state.smart_proxy_enabled = original_smart_proxy_enabled
    state.active_transfers.clear()
    state.transfer_tasks.pop("t1", None)
    state.claimed_download_paths.clear()


@pytest.mark.asyncio
async def test_mac_mismatch_downgraded_to_done_when_verification_disabled(clean_transfer_state):
    state.verify_download_mac = False
    state.active_transfers["t1"] = {"id": "t1", "kind": "download", "link": "l", "name": None, "status": "starting", "bytes_done": 0, "total": 0}

    fake_api = FakeApi(FakeMeta(name="file.bin", size=100))
    fake_downloader = AsyncMock()
    fake_downloader.run.return_value = FakeResult(mac_verified=False)

    with (
        patch("app.api.routes_transfers.MegaAPI", return_value=fake_api),
        patch("app.api.routes_transfers.Downloader", return_value=fake_downloader),
    ):
        await _run_download("t1", "https://mega.nz/file/x#y")

    assert state.active_transfers["t1"]["status"] == "done"


@pytest.mark.asyncio
async def test_mac_mismatch_reported_when_verification_enabled(clean_transfer_state):
    state.verify_download_mac = True
    state.active_transfers["t1"] = {"id": "t1", "kind": "download", "link": "l", "name": None, "status": "starting", "bytes_done": 0, "total": 0}

    fake_api = FakeApi(FakeMeta(name="file2.bin", size=100))
    fake_downloader = AsyncMock()
    fake_downloader.run.return_value = FakeResult(mac_verified=False)

    with (
        patch("app.api.routes_transfers.MegaAPI", return_value=fake_api),
        patch("app.api.routes_transfers.Downloader", return_value=fake_downloader),
    ):
        await _run_download("t1", "https://mega.nz/file/x#y")

    assert state.active_transfers["t1"]["status"] == "mac_mismatch"


@pytest.mark.asyncio
async def test_run_download_never_proxies_the_metadata_lookup(clean_transfer_state):
    """Regression test: only the actual chunk-byte transfer (Downloader)
    should route through SmartProxy. The MegaAPI instance used for the
    file-info/download-URL lookup must always be constructed with
    proxy_manager=None, even when SmartProxy is enabled -- otherwise a
    bad/slow proxy can break folder browsing and download initiation,
    not just the transfer itself."""
    state.active_transfers["t1"] = {"id": "t1", "kind": "download", "link": "l", "name": None, "status": "starting", "bytes_done": 0, "total": 0}
    state.smart_proxy_enabled = True

    fake_api = FakeApi(FakeMeta(name="file4.bin", size=100))
    fake_downloader = AsyncMock()
    fake_downloader.run.return_value = FakeResult(mac_verified=True)

    with (
        patch("app.api.routes_transfers.MegaAPI", return_value=fake_api) as mock_mega_api,
        patch("app.api.routes_transfers.Downloader", return_value=fake_downloader) as mock_downloader,
    ):
        await _run_download("t1", "https://mega.nz/file/x#y")

    assert mock_mega_api.call_args.kwargs["proxy_manager"] is None
    # Downloader (the actual byte transfer) still gets a real proxy_manager.
    assert mock_downloader.call_args.kwargs["proxy_manager"] is state.proxy_manager


def test_update_speed_seeds_baseline_then_computes(monkeypatch):
    """Regression test: the first progress callback must seed the speed
    tracker with (now, bytes) so later samples have a baseline to measure
    against. The old code took the current values as the .get() default and
    returned early without storing them, so dt stayed ~0 forever and speed
    was permanently 0 -- the per-item/global speed readouts never worked."""
    from app.api import routes_transfers as rt

    rt._speed_trackers.pop("spd", None)
    clock = {"t": 100.0}
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])

    entry = {"speed": 0.0}
    # First sample only seeds the baseline; nothing to measure yet.
    rt._update_speed("spd", entry, 0)
    assert rt._speed_trackers["spd"] == (100.0, 0)
    assert entry["speed"] == 0.0

    # One second later, 1 MiB has moved -> 1 MiB/s instantaneous, smoothed.
    clock["t"] = 101.0
    rt._update_speed("spd", entry, 1_048_576)
    assert entry["speed"] == pytest.approx(rt._SPEED_SMOOTHING * 1_048_576)

    rt._speed_trackers.pop("spd", None)


@pytest.mark.asyncio
async def test_successful_mac_is_always_done_regardless_of_setting(clean_transfer_state):
    state.verify_download_mac = True
    state.active_transfers["t1"] = {"id": "t1", "kind": "download", "link": "l", "name": None, "status": "starting", "bytes_done": 0, "total": 0}

    fake_api = FakeApi(FakeMeta(name="file3.bin", size=100))
    fake_downloader = AsyncMock()
    fake_downloader.run.return_value = FakeResult(mac_verified=True)

    with (
        patch("app.api.routes_transfers.MegaAPI", return_value=fake_api),
        patch("app.api.routes_transfers.Downloader", return_value=fake_downloader),
    ):
        await _run_download("t1", "https://mega.nz/file/x#y")

    assert state.active_transfers["t1"]["status"] == "done"
