import asyncio
import os
from unittest.mock import patch

import httpx
import pytest

from app.core import crypto
from app.core.chunks import iter_chunks
from app.transfers.mac import FileMacGenerator
from app.transfers.upload import Uploader, build_node_key


class FakeApi:
    """Duck-types the two MegaAPI methods Uploader calls, recording what
    finish_upload_file receives so the test can assert on the exact key
    material sent, without hitting the real MEGA network."""

    def __init__(self, ul_url: str):
        self.master_key = bytes(16)
        self._ul_url = ul_url
        self.finish_calls: list[dict] = []

    async def init_upload_file(self, file_size: int) -> str:
        return self._ul_url

    async def finish_upload_file(
        self,
        file_basename,
        ul_key_words,
        fkey_words,
        completion_handle,
        mega_parent,
        master_key,
        root_node,
        share_key=None,
    ):
        self.finish_calls.append(
            dict(
                file_basename=file_basename,
                ul_key_words=ul_key_words,
                fkey_words=fkey_words,
                completion_handle=completion_handle,
                mega_parent=mega_parent,
                master_key=master_key,
                root_node=root_node,
                share_key=share_key,
            )
        )
        return {"f": [{"h": "NEWHANDLE123"}]}


@pytest.mark.asyncio
async def test_upload_encrypts_and_reassembles_chunks_correctly(tmp_path):
    size = 3584 * 1024 + 5 * 1024 * 1024  # spans geometric + fixed-size chunks
    plaintext = os.urandom(size)
    src = tmp_path / "src.bin"
    src.write_bytes(plaintext)

    fixed_key_words = [100, 200, 300, 400, 500, 600]
    received: dict[int, bytes] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        received[start] = request.content
        end_reached = start + len(request.content) == size
        return httpx.Response(200, content=b"COMPLETIONHANDLE" if end_reached else b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = FakeApi(ul_url="http://fake.invalid/up/handle")

    with patch("app.transfers.upload.gen_upload_key", return_value=fixed_key_words):
        uploader = Uploader(api, str(src), parent_node="PARENT", slots=8, client=client)
        result = await uploader.run()

    assert result.node_handle == "NEWHANDLE123"
    assert len(api.finish_calls) == 1
    call = api.finish_calls[0]
    assert call["ul_key_words"] == fixed_key_words
    assert call["completion_handle"] == "COMPLETIONHANDLE"
    assert call["mega_parent"] == "PARENT"
    assert call["root_node"] == "PARENT"  # defaults to parent_node when not given

    aes_key = crypto.i32a2bin(fixed_key_words[:4])
    nonce_words = (fixed_key_words[4], fixed_key_words[5])
    nonce_bytes = crypto.i32a2bin(list(nonce_words))

    # Decrypt everything the mock server actually received off the wire and
    # confirm it reassembles into the exact source file (proves chunking +
    # per-chunk CTR counter offset are correct).
    reconstructed = bytearray(size)
    for chunk in iter_chunks(size, size_multi=1):
        ciphertext = received[chunk.offset]
        assert len(ciphertext) == chunk.size
        pt = crypto.aes_ctr_crypt(ciphertext, aes_key, nonce_bytes, counter_start=chunk.offset // 16)
        reconstructed[chunk.offset : chunk.offset + chunk.size] = pt
    assert bytes(reconstructed) == plaintext

    # And confirm the node key handed to finish_upload_file is exactly what
    # an independent MAC computation over the plaintext would produce.
    mac_gen = FileMacGenerator(aes_key, nonce_words)
    for chunk in iter_chunks(size, size_multi=1):
        mac_gen.process_chunk(plaintext[chunk.offset : chunk.offset + chunk.size])
    expected_node_key = build_node_key(fixed_key_words, mac_gen.meta_mac)
    assert call["fkey_words"] == expected_node_key

    await client.aclose()


@pytest.mark.asyncio
async def test_upload_raises_without_completion_handle(tmp_path):
    size = 200 * 1024
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(size))

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")  # MEGA never sends a completion handle

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = FakeApi(ul_url="http://fake.invalid/up/handle")

    uploader = Uploader(api, str(src), parent_node="PARENT", slots=4, client=client)
    with pytest.raises(RuntimeError, match="completion handle"):
        await uploader.run()

    await client.aclose()


@pytest.mark.asyncio
async def test_upload_cancellation_stops_promptly_and_leaves_no_dangling_tasks(tmp_path):
    size = 20 * 1024 * 1024
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(size))

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = FakeApi(ul_url="http://fake.invalid/up/handle")

    uploader = Uploader(api, str(src), parent_node="PARENT", slots=4, client=client)
    task = asyncio.create_task(uploader.run())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert pending == []

    await client.aclose()


@pytest.mark.asyncio
async def test_upload_bounds_memory_when_one_chunk_stalls(tmp_path):
    """Mirrors the download-side test: a single chunk stuck on its POST
    must not let every later chunk get read off disk, encrypted, and
    buffered in memory unbounded by file size."""
    size = 12 * 1024 * 1024
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(size))

    stall_gate = asyncio.Event()
    requested_offsets = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        requested_offsets.add(start)

        if start == 0:
            await stall_gate.wait()

        end_reached = start + len(request.content) == size
        return httpx.Response(200, content=b"COMPLETIONHANDLE" if end_reached else b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = FakeApi(ul_url="http://fake.invalid/up/handle")

    slots = 3
    uploader = Uploader(api, str(src), parent_node="PARENT", slots=slots, client=client)

    task = asyncio.create_task(uploader.run())
    await asyncio.sleep(0.2)

    assert len(requested_offsets) <= slots, (
        f"expected at most {slots} chunks claimed while chunk 1 stalls, got {len(requested_offsets)}: {sorted(requested_offsets)}"
    )

    stall_gate.set()
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result.node_handle == "NEWHANDLE123"

    await client.aclose()


@pytest.mark.asyncio
async def test_upload_pause_blocks_new_chunks_and_resume_completes(tmp_path):
    size = 12 * 1024 * 1024
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(size))

    requested_offsets = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start = int(suffix.split("-")[0]) if "-" in suffix else int(suffix)
        requested_offsets.add(start)
        end_reached = start + len(request.content) == size
        return httpx.Response(200, content=b"COMPLETIONHANDLE" if end_reached else b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = FakeApi(ul_url="http://fake.invalid/up/handle")

    slots = 3
    pause_event = asyncio.Event()  # starts cleared -- paused before the first chunk
    uploader = Uploader(api, str(src), parent_node="PARENT", slots=slots, client=client, pause_event=pause_event)

    task = asyncio.create_task(uploader.run())
    await asyncio.sleep(0.2)

    assert requested_offsets == set(), f"expected zero requests while paused, got {sorted(requested_offsets)}"

    pause_event.set()
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result.node_handle == "NEWHANDLE123"

    await client.aclose()
