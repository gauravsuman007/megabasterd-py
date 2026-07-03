"""Async parallel-chunk file upload, ported from Upload.java /
ChunkUploader.java / UploadMACGenerator.java.

Mirrors download.py's shape: chunks are encrypted and POSTed concurrently
(bounded by `slots`), but must be folded into the running file-MAC in
strict chunk order -- same reorder-buffer approach, just fed from local
disk reads instead of network reads.

Key material: MEGA generates a random 6-word (24-byte) upload key per file
-- the first 4 words are the *real* AES-CTR key used directly (no XOR
games during encryption), the last 2 are the CTR nonce. Only when
assembling the final node key for storage does it get combined with the
computed meta_mac into the 8-word obfuscated form MEGA actually stores
(see build_node_key) -- confirmed against Upload.java:947-949.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass

import httpx

from app.core import crypto
from app.core.chunks import Chunk, gen_chunk_url, iter_chunks
from app.core.mega_api import MegaAPI, wait_time_exp_backoff
from app.core.proxy_manager import SmartProxyManager
from app.transfers.mac import FileMacGenerator

DEFAULT_SLOTS = 4
MAX_CHUNK_RETRIES = 10


def gen_upload_key() -> list[int]:
    """Random 6-word (24-byte) per-file key: [aes_key(4 words), nonce(2 words)]."""
    return crypto.bin2i32a(secrets.token_bytes(24))


def build_node_key(ul_key_words: list[int], meta_mac: tuple[int, int]) -> list[int]:
    """The 8-word obfuscated node key MEGA stores: the real key XORed with
    (nonce, nonce, mac, mac), followed by the raw nonce and mac words --
    the same layout init_mega_link_key/init_mega_link_key_iv decode on the
    download side."""
    nonce0, nonce1 = ul_key_words[4], ul_key_words[5]
    mac0, mac1 = meta_mac
    return [
        ul_key_words[0] ^ nonce0,
        ul_key_words[1] ^ nonce1,
        ul_key_words[2] ^ mac0,
        ul_key_words[3] ^ mac1,
        nonce0,
        nonce1,
        mac0,
        mac1,
    ]


@dataclass
class UploadResult:
    """Outcome of a completed upload: the new node's `node_handle` (if MEGA
    returned one), the file `size`, and the raw `finish_upload_file` response."""
    node_handle: str | None
    size: int
    raw_response: dict


class Uploader:
    """Uploads one local file to MEGA: encrypts chunks with a fresh per-file key
    and POSTs them concurrently (bounded by `slots`), folds them into the file
    MAC in order, then registers the node with the obfuscated key. Mirrors
    `Downloader` (pause via `pause_event`, SmartProxy 509 rerouting). Await `run()`.
    """

    def __init__(
        self,
        api: MegaAPI,
        file_path: str,
        parent_node: str,
        *,
        root_node: str | None = None,
        share_key: bytes | None = None,
        slots: int = DEFAULT_SLOTS,
        client: httpx.AsyncClient | None = None,
        progress_cb=None,
        proxy_manager: SmartProxyManager | None = None,
        pause_event: asyncio.Event | None = None,
    ):
        self.api = api
        self.file_path = file_path
        self.parent_node = parent_node
        self.root_node = root_node or parent_node
        self.share_key = share_key
        self.slots = max(1, slots)
        self._client = client
        self._owns_client = client is None
        self.progress_cb = progress_cb
        self.proxy_manager = proxy_manager

        # See Downloader's identical field: cleared to pause new chunk
        # reads/uploads from starting, set (the default) to run freely.
        self.pause_event = pause_event or asyncio.Event()
        if pause_event is None:
            self.pause_event.set()

        # See Downloader's identical fields: one proxy shared across all
        # chunk workers for this upload once a 509 forces a switch.
        self._proxy_client: httpx.AsyncClient | None = None
        self._proxy_address: str | None = None
        self._proxy_lock = asyncio.Lock()

    async def _switch_proxy(self, cause: str) -> None:
        """Ban the current proxy and route this upload through a new one, shared
        by all its chunk workers (see Downloader._switch_proxy). Raises if none
        is available."""
        async with self._proxy_lock:
            if self._proxy_address is not None:
                self.proxy_manager.block_proxy(self._proxy_address, cause)
            picked = self.proxy_manager.pick_proxy({self._proxy_address} if self._proxy_address else None)
            old_client = self._proxy_client
            if picked is None:
                self._proxy_address = None
                self._proxy_client = None
            else:
                address, proxy_type = picked
                url = self.proxy_manager.build_proxy_url(address, proxy_type == "socks")
                self._proxy_client = httpx.AsyncClient(proxy=url, timeout=self.proxy_manager.proxy_timeout)
                self._proxy_address = address
            if old_client is not None:
                await old_client.aclose()
            if picked is None:
                raise RuntimeError("SmartProxy: no proxy available to route around HTTP 509")

    async def _post_chunk(self, url: str, data: bytes) -> str:
        """POST one encrypted chunk, retrying with backoff on transient errors
        and rerouting through a new proxy on 509. Returns the response text
        (the completion handle on the last chunk, empty otherwise)."""
        attempt = 0
        while True:
            client = self._proxy_client or self._client
            try:
                resp = await client.post(url, content=data, timeout=60.0)
                resp.raise_for_status()
                return resp.text.strip()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 509 and self.proxy_manager is not None:
                    await self._switch_proxy("HTTP 509")
                    continue
                attempt += 1
                if attempt >= MAX_CHUNK_RETRIES:
                    raise
                await asyncio.sleep(wait_time_exp_backoff(attempt))
            except httpx.HTTPError:
                attempt += 1
                if attempt >= MAX_CHUNK_RETRIES:
                    raise
                await asyncio.sleep(wait_time_exp_backoff(attempt))

    async def run(self) -> UploadResult:
        """Run the whole upload and return an `UploadResult`.

        Reserves an upload URL, generates the per-file key, then reads/encrypts/
        POSTs chunks concurrently while a MAC loop consumes them in order (a
        `claim_sem` caps how many are buffered at once). Once every chunk is in
        and the completion handle is known, computes the meta MAC, builds the
        obfuscated node key, and registers the node. Cancels in-flight POSTs on
        any early exit."""
        owns_client = self._owns_client
        client = self._client or httpx.AsyncClient()
        self._client = client
        try:
            file_size = os.path.getsize(self.file_path)
            ul_url = await self.api.init_upload_file(file_size)

            ul_key_words = gen_upload_key()
            aes_key = crypto.i32a2bin(ul_key_words[:4])
            nonce_words = (ul_key_words[4], ul_key_words[5])
            nonce_bytes = crypto.i32a2bin(list(nonce_words))

            chunks = list(iter_chunks(file_size, size_multi=1))  # uploads are always size_multi=1
            total_chunks = len(chunks)

            # See Downloader.run's identical claim_sem: bounds how many
            # chunks may be read-from-disk-and-in-flight at once, with the
            # permit only released once the MAC loop below has consumed
            # that chunk -- not as soon as its POST finishes. Otherwise a
            # single chunk stuck behind a bad proxy lets every later chunk
            # get read off disk and buffered in memory unbounded by file
            # size (previously this wasn't even gated on the POST, since
            # the disk read happened before the semaphore wait at all).
            claim_sem = asyncio.Semaphore(self.slots)
            pending: dict[int, bytes] = {}
            cond = asyncio.Condition()
            first_error: Exception | None = None
            completion_handle: str | None = None

            async def upload_one(chunk: Chunk) -> None:
                nonlocal first_error, completion_handle
                await claim_sem.acquire()
                await self.pause_event.wait()
                try:
                    plaintext = await asyncio.to_thread(self._read_chunk, chunk)
                    ciphertext = crypto.aes_ctr_crypt(plaintext, aes_key, nonce_bytes, counter_start=chunk.offset // 16)
                    url = gen_chunk_url(ul_url, file_size, chunk.offset, chunk.size)
                    handle = await self._post_chunk(url, ciphertext)
                    if handle:
                        completion_handle = handle
                except Exception as exc:  # noqa: BLE001 - surfaced to the MAC loop below
                    claim_sem.release()  # this chunk will never reach the MAC loop, free its claim now
                    async with cond:
                        if first_error is None:
                            first_error = exc
                        cond.notify_all()
                    return
                async with cond:
                    pending[chunk.chunk_id] = plaintext
                    cond.notify_all()
                # claim_sem stays held until the MAC loop consumes this chunk below

            tasks = [asyncio.create_task(upload_one(c)) for c in chunks]

            mac_gen = FileMacGenerator(aes_key, nonce_words)
            bytes_done = 0
            next_id = 1
            try:
                while next_id <= total_chunks:
                    async with cond:
                        while next_id not in pending and first_error is None:
                            await cond.wait()
                        if first_error is not None:
                            raise first_error
                        plaintext = pending.pop(next_id)

                    mac_gen.process_chunk(plaintext)
                    bytes_done += len(plaintext)
                    if self.progress_cb:
                        self.progress_cb(bytes_done, file_size)
                    next_id += 1
                    claim_sem.release()
            finally:
                # On cancellation (or any early exit) stop waiting for
                # in-flight chunk POSTs to time out naturally, and make sure
                # every task is awaited so none are left dangling.
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            if completion_handle is None:
                raise RuntimeError("upload finished all chunks but MEGA never returned a completion handle")

            meta_mac = mac_gen.meta_mac
            node_key_words = build_node_key(ul_key_words, meta_mac)

            raw_response = await self.api.finish_upload_file(
                os.path.basename(self.file_path),
                ul_key_words,
                node_key_words,
                completion_handle,
                self.parent_node,
                self.api.master_key,
                self.root_node,
                share_key=self.share_key,
            )
            node_handle = None
            nodes = raw_response.get("f") if isinstance(raw_response, dict) else None
            if nodes:
                node_handle = nodes[0].get("h")

            return UploadResult(node_handle=node_handle, size=file_size, raw_response=raw_response)
        finally:
            if self._proxy_client is not None:
                await self._proxy_client.aclose()
            if owns_client:
                await client.aclose()

    def _read_chunk(self, chunk: Chunk) -> bytes:
        """Read one chunk's plaintext from the source file (runs in a thread)."""
        with open(self.file_path, "rb") as f:
            f.seek(chunk.offset)
            return f.read(chunk.size)
