from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from app.core import crypto
from app.core.mega_api import FileMetadata
from app.streaming.video_proxy import _parse_range, router


def test_parse_range_no_header():
    assert _parse_range(None, 1000) == (0, 999)


def test_parse_range_open_ended():
    assert _parse_range("bytes=500-", 1000) == (500, 999)


def test_parse_range_explicit():
    assert _parse_range("bytes=100-199", 1000) == (100, 199)


def test_parse_range_clamps_end_to_file_size():
    assert _parse_range("bytes=0-99999", 1000) == (0, 999)


class FakeApi:
    def __init__(self, meta: FileMetadata, download_url: str):
        self._meta = meta
        self._download_url = download_url

    async def get_mega_file_metadata(self, link: str) -> FileMetadata:
        return self._meta

    async def get_mega_file_download_url(self, link: str) -> str:
        return self._download_url

    async def aclose(self):
        pass


def _build_test_app():
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_stream_endpoint_decrypts_arbitrary_range_correctly():
    aes_key_words = [1, 2, 3, 4]
    nonce_words = (5, 6)
    aes_key = crypto.i32a2bin(aes_key_words)
    nonce_bytes = crypto.i32a2bin(list(nonce_words))

    size = 200_000
    plaintext = bytes((i * 7) % 256 for i in range(size))
    ciphertext = crypto.aes_ctr_crypt(plaintext, aes_key, nonce_bytes, counter_start=0)

    # Build the obfuscated link key the same way real MEGA does.
    meta_mac = (0, 0)  # streaming never checks the MAC, so any placeholder works
    obfuscation = [nonce_words[0], nonce_words[1], meta_mac[0], meta_mac[1]]
    obfuscated_key = [aes_key_words[i] ^ obfuscation[i] for i in range(4)]
    fkey_words = obfuscated_key + list(nonce_words) + list(meta_mac)
    file_key_b64 = crypto.bin_to_url_base64(crypto.i32a2bin(fkey_words))

    meta = FileMetadata(name="clip.mp4", size=size, file_key=file_key_b64)

    async def upstream_handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        start_s, end_s = suffix.split("-")
        start, end = int(start_s), int(end_s)
        return httpx.Response(200, content=ciphertext[start : end + 1])

    fake_api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")
    app = _build_test_app()

    # Not 16-byte aligned on purpose -- exercises the discard-leading-bytes path.
    range_start, range_end = 100_003, 150_002

    # Construct the ASGI test client with the *real* httpx.AsyncClient first --
    # patching app.streaming.video_proxy.httpx.AsyncClient patches the
    # attribute on the shared httpx module object itself (video_proxy.httpx
    # *is* the httpx module, not a copy), so it would otherwise also hijack
    # this client's own construction if done inside the patch context.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("app.streaming.video_proxy.MegaAPI", return_value=fake_api),
            patch(
                "app.streaming.video_proxy.httpx.AsyncClient",
                return_value=httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler)),
            ),
        ):
            resp = await client.get(
                "/stream",
                params={"link": "https://mega.nz/file/x#y"},
                headers={"Range": f"bytes={range_start}-{range_end}"},
            )

    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {range_start}-{range_end}/{size}"
    assert resp.content == plaintext[range_start : range_end + 1]


@pytest.mark.asyncio
async def test_stream_info_endpoint():
    meta = FileMetadata(name="clip.mkv", size=12345, file_key="whatever")
    fake_api = FakeApi(meta, download_url="http://fake.invalid/dl/handle")
    app = _build_test_app()

    with patch("app.streaming.video_proxy.MegaAPI", return_value=fake_api):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/stream/info", params={"link": "https://mega.nz/file/x#y"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "clip.mkv"
    assert data["size"] == 12345
    assert data["content_type"] == "video/x-matroska"
