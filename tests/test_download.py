import asyncio
import os

import httpx
import pytest

from app.core import crypto
from app.core.mega_api import FileMetadata
from app.transfers.download import Downloader
from app.transfers.mac import FileMacGenerator


class FakeApi:
    """Duck-types the two MegaAPI methods Downloader actually calls, so the
    test can exercise the real chunked fetch/decrypt/reorder/MAC pipeline
    against a synthetic in-process HTTP transport instead of the real MEGA
    network (no bandwidth cost, deterministic, can force out-of-order
    completions to prove the reorder buffer actually reorders)."""

    def __init__(self, meta: FileMetadata, download_url: str):
        self._meta = meta
        self._download_url = download_url

    async def get_mega_file_metadata(self, link: str) -> FileMetadata:
        return self._meta

    async def get_mega_file_download_url(self, link: str) -> str:
        return self._download_url


def _build_synthetic_file(size: int, aes_key_words: list[int], nonce_words: tuple[int, int]) -> tuple[bytes, bytes, str]:
    """Returns (plaintext, ciphertext, file_key_b64).

    MEGA's 32-byte link/node key is NOT the raw AES key -- it's
    ``[aes_key XOR (nonce0,nonce1,mac0,mac1)] || [nonce0,nonce1] || [mac0,mac1]``
    (see CryptTools.initMEGALinkKey). The real AES key only comes out after
    XOR-recombining the first and second halves, and the MAC depends on
    encrypting the plaintext with that same real AES key -- so the key
    blob has to be assembled in this order, not by patching MAC words onto
    an already-derived key (that was the bug caught by the failing test
    this fixture was written for).
    """
    aes_key = crypto.i32a2bin(aes_key_words)
    nonce_bytes = crypto.i32a2bin(list(nonce_words))

    plaintext = os.urandom(size)
    ciphertext = crypto.aes_ctr_crypt(plaintext, aes_key, nonce_bytes, counter_start=0)

    from app.core.chunks import iter_chunks

    mac_gen = FileMacGenerator(aes_key, nonce_words)
    for chunk in iter_chunks(size, size_multi=1):
        mac_gen.process_chunk(plaintext[chunk.offset : chunk.offset + chunk.size])
    meta_mac = mac_gen.meta_mac

    obfuscation = [nonce_words[0], nonce_words[1], meta_mac[0], meta_mac[1]]
    obfuscated_key = [aes_key_words[i] ^ obfuscation[i] for i in range(4)]
    fkey_words = obfuscated_key + list(nonce_words) + list(meta_mac)
    file_key_b64 = crypto.bin_to_url_base64(crypto.i32a2bin(fkey_words))

    return plaintext, ciphertext, file_key_b64


def _make_handler(ciphertext: bytes, delay_first_chunk: bool):
    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        if "-" in suffix:
            start_s, end_s = suffix.split("-")
            start, end = int(start_s), int(end_s)
            data = ciphertext[start : end + 1]
        else:
            start = int(suffix)
            data = ciphertext[start:]

        if delay_first_chunk and start == 0:
            # Force chunk 1 to be the *last* to complete, so the writer loop
            # must buffer every later chunk and only flush once this one
            # finally arrives -- proves the reorder buffer actually reorders
            # rather than happening to receive things in order.
            await asyncio.sleep(0.2)

        return httpx.Response(200, content=data)

    return handler


@pytest.mark.asyncio
async def test_download_reassembles_out_of_order_chunks_and_verifies_mac(tmp_path):
    aes_key_words = [11, 22, 33, 44]
    nonce_words = (55, 66)
    size = 3584 * 1024 + 5 * 1024 * 1024  # spans geometric chunks 1-7 plus several fixed 1MB chunks
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")

    handler = _make_handler(ciphertext, delay_first_chunk=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    dest = tmp_path / "out.bin"
    progress_events = []

    downloader = Downloader(
        api=api,
        link="https://mega.nz/file/whatever#whatever",
        dest_path=str(dest),
        slots=8,
        size_multi=1,
        client=client,
        progress_cb=lambda done, total: progress_events.append((done, total)),
    )

    result = await downloader.run()

    assert result.mac_verified is True
    assert result.size == size
    assert dest.read_bytes() == plaintext
    assert progress_events[-1] == (size, size)
    # Progress must have been reported strictly in order (monotonically
    # increasing), proving chunks were flushed in sequence, not as they
    # completed.
    assert [d for d, _ in progress_events] == sorted(d for d, _ in progress_events)

    await client.aclose()


@pytest.mark.asyncio
async def test_ram_saver_produces_identical_output_and_mac(tmp_path):
    """RAM-saver mode (chunks staged to temp files, streamed-decrypt, re-read in
    order) must yield byte-identical output and the same verified MAC as the
    in-RAM path -- even with the first chunk forced to complete last -- and must
    leave no temp files behind on success."""
    aes_key_words = [11, 22, 33, 44]
    nonce_words = (55, 66)
    size = 3584 * 1024 + 5 * 1024 * 1024  # geometric chunks 1-7 + several 1 MB chunks
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_make_handler(ciphertext, delay_first_chunk=True)))

    dest = tmp_path / "out.bin"
    progress_events = []
    downloader = Downloader(
        api=api, link="link", dest_path=str(dest), slots=8, size_multi=1,
        client=client, ram_saver=True,
        progress_cb=lambda done, total: progress_events.append(done),
    )

    result = await downloader.run()

    assert result.mac_verified is True
    assert result.size == size
    assert dest.read_bytes() == plaintext
    assert progress_events[-1] == size
    assert progress_events == sorted(progress_events)  # never regressed
    # No staged temp files left over after a clean run.
    assert list(tmp_path.glob("*.mbtmp.*")) == []

    await client.aclose()


@pytest.mark.asyncio
async def test_ram_saver_cleans_up_temp_files_on_cancellation(tmp_path):
    """A cancelled RAM-saver download must not leave staged .mbtmp.* files behind."""
    aes_key_words = [1, 2, 3, 4]
    nonce_words = (5, 6)
    size = 20 * 1024 * 1024
    _plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        # Let a few chunks stage to disk, then hang so cancellation interrupts
        # mid-flight with temp files already on disk.
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        if start != 0:
            await release.wait()
        end_s = suffix.split("-")[1] if "-" in suffix else None
        end = int(end_s) if end_s else size - 1
        return httpx.Response(200, content=ciphertext[start : end + 1])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")

    dest = tmp_path / "out.bin"
    downloader = Downloader(api=api, link="link", dest_path=str(dest), slots=4, size_multi=1, client=client, ram_saver=True)

    task = asyncio.create_task(downloader.run())
    await asyncio.sleep(0.1)  # let chunk 1 stage its temp file
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert list(tmp_path.glob("*.mbtmp.*")) == []  # swept clean on cancel

    await client.aclose()


@pytest.mark.asyncio
async def test_download_resume_keeps_prefix_and_only_fetches_missing_chunks(tmp_path):
    """A partial file left by a crash must be resumed: its whole-chunk prefix
    is kept (and folded back into the MAC), only the missing chunks are
    re-fetched, and the finished file still verifies byte-for-byte."""
    from app.core.chunks import iter_chunks

    aes_key_words = [11, 22, 33, 44]
    nonce_words = (55, 66)
    size = 3584 * 1024 + 3 * 1024 * 1024  # geometric chunks 1-7 + a few 1MB chunks
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    chunks = list(iter_chunks(size, size_multi=1))
    # Pretend the first 4 chunks finished before the crash.
    prefix_chunks = 4
    prefix_bytes = sum(c.size for c in chunks[:prefix_chunks])

    dest = tmp_path / "out.bin"
    dest.write_bytes(plaintext[:prefix_bytes])

    requested_offsets = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        requested_offsets.add(start)
        end_s = suffix.split("-")[1] if "-" in suffix else None
        end = int(end_s) if end_s else size - 1
        return httpx.Response(200, content=ciphertext[start : end + 1])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")

    progress_events = []
    downloader = Downloader(
        api=api, link="link", dest_path=str(dest), slots=4, size_multi=1,
        client=client, resume=True, progress_cb=lambda d, t: progress_events.append(d),
    )

    result = await downloader.run()

    assert result.mac_verified is True
    assert dest.read_bytes() == plaintext
    # None of the already-present chunks were re-requested.
    assert all(off >= prefix_bytes for off in requested_offsets)
    assert min(requested_offsets) == prefix_bytes  # resumed exactly at the boundary
    # Progress picks up from the resumed offset, not from zero.
    assert progress_events[0] > prefix_bytes

    await client.aclose()


@pytest.mark.asyncio
async def test_download_resume_truncates_torn_partial_chunk(tmp_path):
    """If the crash left a half-written chunk past the last complete boundary,
    resume must truncate it away and re-fetch that chunk cleanly."""
    from app.core.chunks import iter_chunks

    aes_key_words = [7, 8, 9, 10]
    nonce_words = (5, 6)
    size = 3584 * 1024 + 2 * 1024 * 1024
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    chunks = list(iter_chunks(size, size_multi=1))
    prefix_chunks = 3
    prefix_bytes = sum(c.size for c in chunks[:prefix_chunks])

    dest = tmp_path / "out.bin"
    # Whole prefix + a torn partial slice of the next chunk (garbage tail).
    dest.write_bytes(plaintext[:prefix_bytes] + b"\x00" * 123)

    requested_offsets = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        requested_offsets.add(start)
        end_s = suffix.split("-")[1] if "-" in suffix else None
        end = int(end_s) if end_s else size - 1
        return httpx.Response(200, content=ciphertext[start : end + 1])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")

    downloader = Downloader(api=api, link="link", dest_path=str(dest), slots=4, size_multi=1, client=client, resume=True)
    result = await downloader.run()

    assert result.mac_verified is True
    assert dest.read_bytes() == plaintext  # torn tail discarded, chunk re-fetched
    assert min(requested_offsets) == prefix_bytes

    await client.aclose()


@pytest.mark.asyncio
async def test_download_detects_mac_mismatch_on_tampered_data(tmp_path):
    aes_key_words = [1, 2, 3, 4]
    nonce_words = (5, 6)
    size = 500 * 1024
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    # Corrupt one byte of ciphertext so the decrypted output no longer
    # matches the meta_mac embedded in the key.
    tampered = bytearray(ciphertext)
    tampered[1000] ^= 0xFF
    ciphertext = bytes(tampered)

    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_make_handler(ciphertext, delay_first_chunk=False)))

    dest = tmp_path / "out.bin"
    downloader = Downloader(api=api, link="link", dest_path=str(dest), slots=4, size_multi=1, client=client)

    result = await downloader.run()

    assert result.mac_verified is False

    await client.aclose()


@pytest.mark.asyncio
async def test_download_cancellation_stops_promptly_and_leaves_no_dangling_tasks(tmp_path):
    aes_key_words = [1, 2, 3, 4]
    nonce_words = (5, 6)
    size = 20 * 1024 * 1024  # large enough that cancellation mid-flight is meaningful
    _plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    async def handler(request: httpx.Request) -> httpx.Response:
        # Every chunk fetch hangs "forever" (from the test's perspective) so
        # cancellation has something real to interrupt instead of racing
        # against requests that would've finished anyway.
        await asyncio.sleep(10)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")

    dest = tmp_path / "out.bin"
    downloader = Downloader(api=api, link="link", dest_path=str(dest), slots=4, size_multi=1, client=client)

    task = asyncio.create_task(downloader.run())
    await asyncio.sleep(0.05)  # let it get into the semaphore/fetch loop
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)  # must NOT hang waiting on the 10s sleeps

    # No leftover chunk-fetch tasks still running in the background.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert pending == []

    await client.aclose()


@pytest.mark.asyncio
async def test_download_reroutes_through_smartproxy_on_509(tmp_path):
    from unittest.mock import patch

    from app.core.proxy_manager import SmartProxyManager

    aes_key_words = [11, 22, 33, 44]
    nonce_words = (55, 66)
    size = 500 * 1024  # a few chunks, small enough to keep the test fast
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    async def direct_509(request: httpx.Request) -> httpx.Response:
        return httpx.Response(509)

    good_handler = _make_handler(ciphertext, delay_first_chunk=False)

    direct_client = httpx.AsyncClient(transport=httpx.MockTransport(direct_509))
    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(good_handler))

    mgr = SmartProxyManager(random_select=False)

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("1.2.3.4:8080\n", fetch)

    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")
    dest = tmp_path / "out.bin"

    downloader = Downloader(
        api=api, link="link", dest_path=str(dest), slots=4, size_multi=1, client=direct_client, proxy_manager=mgr
    )

    with patch("app.transfers.download.httpx.AsyncClient", return_value=proxy_client):
        result = await downloader.run()

    assert result.mac_verified is True
    assert dest.read_bytes() == plaintext
    # The direct-path proxy was picked and used successfully, never blocked.
    assert mgr.count_blocked() == 0

    await direct_client.aclose()
    await proxy_client.aclose()


@pytest.mark.asyncio
async def test_download_bounds_memory_when_one_chunk_stalls(tmp_path):
    """A single chunk stuck behind a slow/bad proxy must not let every
    later chunk race ahead, decrypt, and buffer in memory unbounded by
    file size -- only `slots` chunks may ever be claimed (in flight or
    fetched-but-unwritten) at once."""
    aes_key_words = [11, 22, 33, 44]
    nonce_words = (55, 66)
    size = 12 * 1024 * 1024  # enough chunks (geometric + several 1MB) to make the point
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    stall_gate = asyncio.Event()
    requested_offsets = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        requested_offsets.add(start)

        if start == 0:  # chunk 1 stalls until the test releases it
            await stall_gate.wait()

        end_s = suffix.split("-")[1] if "-" in suffix else None
        end = int(end_s) if end_s else size - 1
        return httpx.Response(200, content=ciphertext[start : end + 1])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")

    slots = 3
    dest = tmp_path / "out.bin"
    downloader = Downloader(api=api, link="link", dest_path=str(dest), slots=slots, size_multi=1, client=client)

    task = asyncio.create_task(downloader.run())
    await asyncio.sleep(0.2)  # let everything that's going to start without chunk 1, start

    # Only `slots` chunks may ever be claimed at once, and chunk 1 (offset 0)
    # is one of them (stalled) -- so at most `slots` distinct offsets should
    # have been requested, never anywhere near the ~20 chunks this file has.
    assert len(requested_offsets) <= slots, (
        f"expected at most {slots} chunks claimed while chunk 1 stalls, got {len(requested_offsets)}: {sorted(requested_offsets)}"
    )

    stall_gate.set()  # release chunk 1, the rest should now proceed to completion
    result = await asyncio.wait_for(task, timeout=5.0)

    assert result.mac_verified is True
    assert dest.read_bytes() == plaintext


@pytest.mark.asyncio
async def test_download_reports_inflight_progress_while_inorder_chunk_stalls(tmp_path):
    """Progress must reflect bytes fetched across all slots, not just the one
    in-order chunk being written. Before this, the bar sat frozen for the
    whole duration of a large in-order chunk (nothing is *written* until it
    lands) even though later chunks were already downloading -- a 20 MB
    chunk made that a minute-long freeze. Here chunk 1 stalls while later
    chunks finish fetching; progress must move above zero regardless."""
    aes_key_words = [11, 22, 33, 44]
    nonce_words = (55, 66)
    size = 3584 * 1024 + 3 * 1024 * 1024  # geometric chunks 1-7 + a few 1MB chunks
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    stall_gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        if start == 0:  # the in-order chunk (written first) stalls
            await stall_gate.wait()
        end_s = suffix.split("-")[1] if "-" in suffix else None
        end = int(end_s) if end_s else size - 1
        return httpx.Response(200, content=ciphertext[start : end + 1])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")

    dest = tmp_path / "out.bin"
    progress_events = []
    downloader = Downloader(
        api=api, link="link", dest_path=str(dest), slots=4, size_multi=1,
        client=client, progress_cb=lambda d, t: progress_events.append(d),
    )

    task = asyncio.create_task(downloader.run())
    await asyncio.sleep(0.3)  # let the non-stalled later chunks fetch

    # Nothing has been *written* (chunk 1 is stalled), yet progress has moved
    # because later chunks' fetched bytes are counted as in flight.
    assert progress_events, "expected in-flight progress while the in-order chunk stalled"
    assert max(progress_events) > 0

    stall_gate.set()
    result = await asyncio.wait_for(task, timeout=5.0)

    assert result.mac_verified is True
    assert dest.read_bytes() == plaintext
    assert progress_events[-1] == size  # ends exactly at full size
    assert progress_events == sorted(progress_events)  # never regressed

    await client.aclose()


@pytest.mark.asyncio
async def test_download_pause_blocks_new_chunks_and_resume_completes(tmp_path):
    """Clearing the pause_event must stop new chunk fetches from starting
    (while anything already mid-flight is left to finish normally), and
    setting it again must let the download proceed to a normal, correct
    completion -- no bytes lost, no chunks skipped or duplicated."""
    aes_key_words = [11, 22, 33, 44]
    nonce_words = (55, 66)
    size = 12 * 1024 * 1024
    plaintext, ciphertext, file_key_b64 = _build_synthetic_file(size, aes_key_words, nonce_words)

    requested_offsets = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        requested_offsets.add(start)
        end_s = suffix.split("-")[1] if "-" in suffix else None
        end = int(end_s) if end_s else size - 1
        return httpx.Response(200, content=ciphertext[start : end + 1])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = FileMetadata(name="synthetic.bin", size=size, file_key=file_key_b64)
    api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")

    slots = 3
    dest = tmp_path / "out.bin"
    pause_event = asyncio.Event()  # starts cleared -- paused from the very first chunk
    downloader = Downloader(api=api, link="link", dest_path=str(dest), slots=slots, size_multi=1, client=client, pause_event=pause_event)

    task = asyncio.create_task(downloader.run())
    await asyncio.sleep(0.2)  # give it every chance to have started chunks if pause weren't working

    assert requested_offsets == set(), f"expected zero requests while paused, got {sorted(requested_offsets)}"

    pause_event.set()  # resume
    result = await asyncio.wait_for(task, timeout=5.0)

    assert result.mac_verified is True
    assert dest.read_bytes() == plaintext

    await client.aclose()
