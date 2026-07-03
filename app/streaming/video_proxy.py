"""Range-aware streaming proxy, ported from KissVideoStreamServer.java /
StreamChunkManager.java (simplified: direct pass-through decryption instead
of a prefetch-window of worker threads -- a single HTTP/1.1 connection with
CTR mode decrypted on the fly is enough for one <video> tag's sequential
reads/seeks, and browsers already parallelize by issuing fresh Range
requests when they seek).

AES-CTR is a stream cipher, so an arbitrary byte range can be decrypted
without needing the chunk-boundary alignment the transfer engine uses for
MAC bookkeeping: fetch from the nearest 16-byte-aligned offset at or before
the requested start, decrypt from there (counter = aligned_offset // 16),
and drop the few leading bytes needed to land exactly on the requested
start. No MAC verification here, matching the Java version (streaming
trades integrity-checking for not having to download/verify the whole
file up front).
"""
from __future__ import annotations

import mimetypes

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from Crypto.Cipher import AES
from Crypto.Util import Counter

from app import state
from app.core import crypto
from app.core.mega_api import MegaAPI

router = APIRouter(tags=["streaming"])

CHUNK_READ_SIZE = 256 * 1024


def _guess_content_type(filename: str) -> str:
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or "application/octet-stream"


def _parse_range(range_header: str | None, file_size: int) -> tuple[int, int]:
    if not range_header or not range_header.startswith("bytes="):
        return 0, file_size - 1
    spec = range_header[len("bytes=") :]
    start_s, _, end_s = spec.partition("-")
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else file_size - 1
    return start, min(end, file_size - 1)


@router.get("/api/stream/info")
async def stream_info(link: str):
    # Metadata lookup only, never proxied -- see _run_download's comment.
    api = MegaAPI(api_key=state.mega_api_key, proxy_manager=None)
    try:
        meta = await api.get_mega_file_metadata(link)
    finally:
        await api.aclose()
    return {"name": meta.name, "size": meta.size, "content_type": _guess_content_type(meta.name)}


@router.get("/stream")
async def stream_video(request: Request, link: str):
    # Metadata lookup only, never proxied -- see _run_download's comment.
    # (The actual streamed bytes below already go direct, unproxied, too.)
    api = MegaAPI(api_key=state.mega_api_key, proxy_manager=None)
    try:
        meta = await api.get_mega_file_metadata(link)
        download_url = await api.get_mega_file_download_url(link)
    finally:
        await api.aclose()

    file_key_bytes = crypto.url_base64_to_bin(meta.file_key)
    aes_key = crypto.init_mega_link_key(file_key_bytes)
    nonce_bytes = crypto.init_mega_link_key_iv(file_key_bytes)

    file_size = meta.size
    range_header = request.headers.get("range")
    start, end = _parse_range(range_header, file_size)
    if start < 0 or start > end or end >= file_size:
        raise HTTPException(416, "Invalid range")

    aligned_start = (start // 16) * 16
    leading_discard = start - aligned_start
    upstream_url = f"{download_url}/{aligned_start}-{end}"

    async def body():
        counter = Counter.new(64, prefix=nonce_bytes, initial_value=aligned_start // 16)
        cipher = AES.new(aes_key, AES.MODE_CTR, counter=counter)
        discard = leading_discard
        async with httpx.AsyncClient() as client, client.stream("GET", upstream_url, timeout=60.0) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_bytes(CHUNK_READ_SIZE):
                plain = cipher.decrypt(raw)
                if discard:
                    plain = plain[discard:]
                    discard = 0
                if plain:
                    yield plain

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type": _guess_content_type(meta.name),
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    status_code = 206 if range_header else 200
    return StreamingResponse(body(), status_code=status_code, headers=headers, media_type=headers["Content-Type"])
