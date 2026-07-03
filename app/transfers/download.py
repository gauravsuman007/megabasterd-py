"""Async parallel-chunk file download, ported from Download.java /
ChunkDownloader.java / ChunkWriterManager.java.

Chunks are fetched concurrently (bounded by `slots`) out of order, but must
be written to disk and folded into the running file-MAC strictly in chunk
order -- a bounded reorder buffer + condition variable stands in for Java's
disk-based rename-and-poll ChunkWriterManager (no need to round-trip
through temp files when everything lives in one process/event loop).
"""
from __future__ import annotations

import asyncio
import glob
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.core import crypto
from app.core.chunks import Chunk, gen_chunk_url, iter_chunks
from app.core.mega_api import MegaAPI, wait_time_exp_backoff
from app.core.proxy_manager import SmartProxyManager
from app.transfers.mac import FileMacGenerator

DEFAULT_SLOTS = 4
DEFAULT_SIZE_MULTI = 20
MAX_CHUNK_RETRIES = 10

# Sub-chunk progress can be reported on every network block that arrives.
# With 20 MB chunks and many slots that would be hundreds of callbacks per
# chunk, each spawning a WebSocket broadcast -- so in-flight progress emits
# are throttled to at most one per this many seconds per download (a forced
# emit still fires on every completed chunk write, so authoritative points
# and the final size are always exact).
_PROGRESS_MIN_INTERVAL = 0.2

ProgressCallback = Callable[[int, int], None]


@dataclass
class DownloadResult:
    """Outcome of a completed download: output `path`, total `size`, and whether
    the recomputed file MAC matched the one embedded in the link key."""
    path: str
    size: int
    mac_verified: bool


class ChunkFetchError(Exception):
    """A chunk came back the wrong size (short/over-read); triggers a retry."""


def _read_and_remove(path: str) -> bytes:
    """Read a staged chunk's plaintext back from its temp file and delete the
    file (RAM-saver mode). Runs in a worker thread off the event loop."""
    with open(path, "rb") as tf:
        data = tf.read()
    os.remove(path)
    return data


class Downloader:
    """Downloads one MEGA file: fetches chunks concurrently (bounded by `slots`),
    decrypts each with AES-CTR, and writes them to `dest_path` in strict chunk
    order while folding them into the file MAC. Supports pause (via a shared
    `pause_event`), partial-file resume, and SmartProxy rerouting on HTTP 509.
    Drive it by awaiting `run()`.

    When `ram_saver` is set, out-of-order chunks are decrypted straight to
    per-chunk temp files (streamed, never held whole in memory) and re-read in
    order by the writer, instead of buffering their plaintext in RAM. This
    keeps peak memory flat as slots/concurrent-downloads climb, at the cost of
    writing each chunk to disk twice (temp + final). See the "RAM saver"
    Advanced setting.
    """

    def __init__(
        self,
        api: MegaAPI,
        link: str,
        dest_path: str,
        *,
        slots: int = DEFAULT_SLOTS,
        size_multi: int = DEFAULT_SIZE_MULTI,
        client: httpx.AsyncClient | None = None,
        progress_cb: ProgressCallback | None = None,
        proxy_manager: SmartProxyManager | None = None,
        pause_event: asyncio.Event | None = None,
        resume: bool = False,
        ram_saver: bool = False,
    ):
        self.api = api
        self.link = link
        self.dest_path = dest_path
        self.slots = max(1, slots)
        self.size_multi = size_multi
        self._client = client
        self._owns_client = client is None
        self.progress_cb = progress_cb
        self.proxy_manager = proxy_manager
        # When True, an already-present dest file is treated as a partial
        # download: its whole-chunk prefix is kept, the file MAC is rebuilt
        # over it, and only the missing chunks are fetched. Only set on a
        # restart-resume -- fresh downloads always get a non-existent dest
        # (see routes_transfers._claim_unique_path), so resume=False can't
        # accidentally clobber-vs-resume the wrong file.
        self.resume = resume
        # See the class docstring: stage out-of-order chunks on disk instead of
        # in RAM. Opt-in (Advanced > RAM saver) -- only pays off at high
        # slots/concurrency, where the in-RAM reorder buffer would otherwise
        # grow as slots * chunk_size * concurrent_downloads.
        self.ram_saver = ram_saver

        # Cleared to pause: new chunk fetches block right after claiming a
        # slot (see `fetch` below) instead of starting real work, while
        # whatever's already mid-flight finishes normally. Set (the
        # default) means "run immediately, never pauses" -- callers that
        # want pause control pass in their own Event and clear/set it.
        self.pause_event = pause_event or asyncio.Event()
        if pause_event is None:
            self.pause_event.set()

        # Shared across all chunk workers: once one hits a 509 and picks a
        # proxy, every subsequent chunk fetch for this download routes
        # through it too (mirrors Java assigning one proxy per download,
        # not renegotiating per chunk). Concurrent 509s from multiple
        # workers can each trigger a switch; the lock serializes those, at
        # the cost of occasionally blocking a proxy that was only ever
        # picked, never actually tried -- an accepted simplification.
        self._proxy_client: httpx.AsyncClient | None = None
        self._proxy_address: str | None = None
        self._proxy_lock = asyncio.Lock()

    async def _switch_proxy(self, cause: str) -> None:
        """Ban the current proxy (if any) and route this download through a new
        one, shared by all its chunk workers. Serialized by `_proxy_lock` so
        concurrent 509s don't thrash. Raises if no proxy is available."""
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

    async def _fetch_range(
        self,
        download_url: str,
        file_size: int,
        chunk: Chunk,
        on_bytes: Callable[[int], None] | None = None,
    ) -> bytes:
        """Fetch one chunk's ciphertext, streaming it (so progress can be
        reported as bytes land), retrying with backoff on transient errors and
        rerouting through a new proxy on 509. `on_bytes(n)` reports in-flight
        progress. Raises after MAX_CHUNK_RETRIES."""
        url = gen_chunk_url(download_url, file_size, chunk.offset, chunk.size)
        attempt = 0
        while True:
            client = self._proxy_client or self._client
            try:
                buf = bytearray()
                need_switch = False
                # Stream the chunk instead of buffering it whole, so the
                # writer/progress side can see bytes as they land (a single
                # post-ramp chunk can be tens of MB). `async with` guarantees
                # the connection is released back to the pool on every exit
                # path -- including the 509/error branches below -- so a slow
                # or failed chunk can't leak connections and stall the pool.
                async with client.stream("GET", url, timeout=60.0) as resp:
                    if resp.status_code == 509 and self.proxy_manager is not None:
                        # Don't switch proxies while this response is still
                        # open; flag it and do the switch after the context
                        # exits, so we never aclose a client mid-stream.
                        need_switch = True
                    else:
                        resp.raise_for_status()
                        if on_bytes is not None:
                            on_bytes(0)  # reset this chunk's in-flight count for a fresh attempt
                        async for block in resp.aiter_bytes():
                            buf += block
                            if on_bytes is not None:
                                on_bytes(len(buf))
                if need_switch:
                    await self._switch_proxy("HTTP 509")
                    continue  # retry immediately through the new proxy, no backoff
                if len(buf) != chunk.size:
                    raise ChunkFetchError(f"chunk {chunk.chunk_id}: expected {chunk.size} bytes, got {len(buf)}")
                return bytes(buf)
            except httpx.HTTPStatusError:
                attempt += 1
                if attempt >= MAX_CHUNK_RETRIES:
                    raise
                await asyncio.sleep(wait_time_exp_backoff(attempt))
            except (httpx.HTTPError, ChunkFetchError):
                attempt += 1
                if attempt >= MAX_CHUNK_RETRIES:
                    raise
                await asyncio.sleep(wait_time_exp_backoff(attempt))

    async def _fetch_range_to_file(
        self,
        download_url: str,
        file_size: int,
        chunk: Chunk,
        tmp_path: str,
        aes_key: bytes,
        nonce_bytes: bytes,
        on_bytes: Callable[[int], None] | None = None,
    ) -> None:
        """RAM-saver variant of `_fetch_range`: stream the chunk's ciphertext,
        CTR-decrypt it block by block, and write the plaintext straight to
        `tmp_path` -- so a whole chunk's plaintext is never resident in memory.
        Same backoff/509-reroute behaviour; the temp file is re-truncated (via
        ``wb``) and the CTR cipher re-seeded at the start of every attempt so a
        retried chunk can't concatenate a torn partial onto a fresh one."""
        url = gen_chunk_url(download_url, file_size, chunk.offset, chunk.size)
        attempt = 0
        while True:
            client = self._proxy_client or self._client
            try:
                need_switch = False
                written = 0
                # Fresh per-attempt cipher: CTR is a stream cipher, so feeding
                # the arbitrarily-sized blocks aiter_bytes yields, in order, is
                # equivalent to decrypting the whole chunk at once.
                cipher = crypto.new_ctr_cipher(aes_key, nonce_bytes, chunk.offset // 16)
                with open(tmp_path, "wb") as tf:
                    async with client.stream("GET", url, timeout=60.0) as resp:
                        if resp.status_code == 509 and self.proxy_manager is not None:
                            need_switch = True  # switch after the stream closes (see _fetch_range)
                        else:
                            resp.raise_for_status()
                            if on_bytes is not None:
                                on_bytes(0)  # reset this chunk's in-flight count for a fresh attempt
                            async for block in resp.aiter_bytes():
                                tf.write(cipher.encrypt(block))  # CTR decrypt straight to disk
                                written += len(block)
                                if on_bytes is not None:
                                    on_bytes(written)
                if need_switch:
                    await self._switch_proxy("HTTP 509")
                    continue
                if written != chunk.size:
                    raise ChunkFetchError(f"chunk {chunk.chunk_id}: expected {chunk.size} bytes, got {written}")
                return
            except httpx.HTTPStatusError:
                attempt += 1
                if attempt >= MAX_CHUNK_RETRIES:
                    raise
                await asyncio.sleep(wait_time_exp_backoff(attempt))
            except (httpx.HTTPError, ChunkFetchError):
                attempt += 1
                if attempt >= MAX_CHUNK_RETRIES:
                    raise
                await asyncio.sleep(wait_time_exp_backoff(attempt))

    def _chunk_tmp_path(self, chunk_id: int) -> str:
        """Temp-file path for a staged chunk (RAM-saver mode). Kept alongside
        the destination so the final in-order write is a same-filesystem move
        of bytes, and so `_cleanup_temps` can find strays by glob."""
        return f"{self.dest_path}.mbtmp.{chunk_id}"

    def _cleanup_temps(self) -> None:
        """Remove any leftover RAM-saver temp files (from a cancel/error that
        left staged-but-unconsumed chunks). A clean run deletes each as it's
        consumed, so normally nothing is left to remove here."""
        for p in glob.glob(f"{self.dest_path}.mbtmp.*"):
            try:
                os.remove(p)
            except OSError:
                pass

    async def run(self) -> DownloadResult:
        """Run the whole download to completion and return a `DownloadResult`.

        Resolves metadata + download URL, derives the key/nonce/expected MAC,
        replays any resumable on-disk prefix, then fetches the remaining chunks
        concurrently while a single writer loop drains them in order to disk and
        into the MAC. A `claim_sem` caps how many chunks are buffered at once so
        a straggler can't pile up plaintext in memory. Cancels in-flight fetches
        on any early exit."""
        owns_client = self._owns_client
        client = self._client or httpx.AsyncClient()
        self._client = client
        try:
            meta = await self.api.get_mega_file_metadata(self.link)
            download_url = await self.api.get_mega_file_download_url(self.link)

            file_key_bytes = crypto.url_base64_to_bin(meta.file_key)
            words = crypto.bin2i32a(file_key_bytes[:32])
            aes_key = crypto.init_mega_link_key(file_key_bytes)
            nonce_words = (words[4], words[5])
            nonce_bytes = crypto.i32a2bin(list(nonce_words))
            expected_meta_mac = (words[6], words[7])

            chunks = list(iter_chunks(meta.size, size_multi=self.size_multi))
            total_chunks = len(chunks)

            mac_gen = FileMacGenerator(aes_key, nonce_words)

            # Resume: keep the largest whole-chunk prefix already on disk and
            # replay it through the MAC generator, so only the chunks that
            # were never finished get re-fetched. Writes are strictly
            # in-order, so the on-disk prefix up to a chunk boundary is
            # always valid; anything past the last complete chunk (a torn
            # final write from a crash) is truncated away.
            resume_bytes = 0
            resume_chunks = 0
            if self.resume and os.path.exists(self.dest_path):
                existing = os.path.getsize(self.dest_path)
                for c in chunks:
                    if resume_bytes + c.size <= existing:
                        resume_bytes += c.size
                        resume_chunks += 1
                    else:
                        break
                if resume_bytes > 0:
                    with open(self.dest_path, "rb") as rf:
                        for c in chunks[:resume_chunks]:
                            await asyncio.to_thread(mac_gen.process_chunk, rf.read(c.size))

            # Bounds how many chunks may be *claimed* at once (being fetched,
            # or already fetched+decrypted but still waiting in `pending`
            # for the writer to catch up to them). The permit for a chunk
            # is only released once the writer has actually written it --
            # not as soon as the fetch finishes -- so a single stalled
            # chunk (e.g. stuck behind a bad proxy) can't let every later
            # chunk race ahead and pile up decrypted plaintext in memory.
            # Without this, memory use is bounded only by file size, not by
            # `slots`; with it, at most `slots` chunks' worth are ever
            # buffered at once, however far a straggler falls behind.
            claim_sem = asyncio.Semaphore(self.slots)
            # id -> plaintext bytes (normal mode) or temp-file path (RAM saver).
            pending: dict[int, bytes | str] = {}
            cond = asyncio.Condition()
            first_error: Exception | None = None

            written_bytes = resume_bytes
            next_id = resume_chunks + 1

            # Bytes fetched-so-far for each claimed-but-not-yet-written chunk
            # (a fully fetched chunk sits here at its full size until the
            # writer folds it in). Reported progress = written + everything
            # in flight, so the bar reflects real download activity across
            # all slots, not just the one in-order chunk being written.
            chunk_inflight: dict[int, int] = {}
            displayed = resume_bytes
            last_emit_ts = 0.0

            def emit_progress(force: bool = False) -> None:
                nonlocal displayed, last_emit_ts
                if self.progress_cb is None:
                    return
                now = time.monotonic()
                if not force and now - last_emit_ts < _PROGRESS_MIN_INTERVAL:
                    return
                computed = written_bytes + sum(chunk_inflight.values())
                # Never regress the bar within a run (a mid-chunk retry resets
                # that chunk's in-flight count to 0, which would otherwise dip
                # the total); the reorder test relies on monotonic progress.
                if computed < displayed:
                    computed = displayed
                # Nothing new to publish (e.g. the 0-byte per-attempt reset):
                # skip the throttled emit, but always honour a forced one so
                # authoritative write points -- including the final size --
                # are emitted even when in-flight already covered them.
                if not force and computed == displayed:
                    return
                displayed = computed
                last_emit_ts = now
                self.progress_cb(displayed, meta.size)

            async def fetch(chunk: Chunk) -> None:
                nonlocal first_error
                await claim_sem.acquire()
                await self.pause_event.wait()

                def note(fetched: int) -> None:
                    chunk_inflight[chunk.chunk_id] = fetched
                    emit_progress()

                try:
                    if self.ram_saver:
                        # Decrypt straight to a temp file; only the path waits
                        # in `pending`, so the reorder buffer holds no plaintext.
                        tmp_path = self._chunk_tmp_path(chunk.chunk_id)
                        await self._fetch_range_to_file(
                            download_url, meta.size, chunk, tmp_path, aes_key, nonce_bytes, on_bytes=note
                        )
                        deposit: bytes | str = tmp_path
                    else:
                        ciphertext = await self._fetch_range(download_url, meta.size, chunk, on_bytes=note)
                        deposit = crypto.aes_ctr_crypt(ciphertext, aes_key, nonce_bytes, counter_start=chunk.offset // 16)
                except Exception as exc:  # noqa: BLE001 - surfaced to the writer loop below
                    claim_sem.release()  # this chunk will never reach the writer, free its claim now
                    chunk_inflight.pop(chunk.chunk_id, None)
                    async with cond:
                        if first_error is None:
                            first_error = exc
                        cond.notify_all()
                    return
                async with cond:
                    pending[chunk.chunk_id] = deposit
                    cond.notify_all()
                # claim_sem stays held until the writer processes this chunk below

            # Only the chunks not already on disk get fetched.
            tasks = [asyncio.create_task(fetch(c)) for c in chunks[resume_chunks:]]

            if resume_bytes > 0:
                f = open(self.dest_path, "r+b")
                f.truncate(resume_bytes)  # drop any torn partial chunk past the boundary
                f.seek(resume_bytes)
            else:
                f = open(self.dest_path, "wb")
            try:
                while next_id <= total_chunks:
                    async with cond:
                        while next_id not in pending and first_error is None:
                            await cond.wait()
                        if first_error is not None:
                            raise first_error
                        item = pending.pop(next_id)

                    if self.ram_saver:
                        # Read the staged plaintext back from disk (and delete
                        # the temp file) just before writing it in order.
                        plaintext = await asyncio.to_thread(_read_and_remove, item)
                    else:
                        plaintext = item

                    await asyncio.to_thread(f.write, plaintext)
                    mac_gen.process_chunk(plaintext)
                    # Move this chunk from "in flight" to "written": its size
                    # was already counted while fetching, so the total stays
                    # flat here (no double count, no dip) -- a forced emit
                    # publishes the authoritative on-disk position.
                    written_bytes += len(plaintext)
                    chunk_inflight.pop(next_id, None)
                    emit_progress(force=True)
                    next_id += 1
                    claim_sem.release()
            finally:
                # On cancellation (or any early exit), stop waiting for
                # in-flight chunk requests to time out naturally -- cancel
                # them so the transfer actually stops promptly.
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                f.close()
                if self.ram_saver:
                    self._cleanup_temps()  # sweep any staged-but-unconsumed chunks

            mac_ok = mac_gen.meta_mac == expected_meta_mac
            return DownloadResult(path=self.dest_path, size=meta.size, mac_verified=mac_ok)
        finally:
            if self._proxy_client is not None:
                await self._proxy_client.aclose()
            if owns_client:
                await client.aclose()
