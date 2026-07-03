"""Chunk size/offset formula, ported from ChunkWriterManager.java.

MEGA splits files into chunks that grow geometrically for the first 7
chunks (128KB, 256KB, ... 896KB) and then settle into a fixed size (1MB,
multiplied by `size_multi` for multi-slot downloads). `chunk_id` is
1-indexed. Chunk byte ranges are requested by appending "/offset-end" to
the storage node URL (MEGA's own convention) -- not a standard HTTP Range
header.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

_GEOMETRIC_OFFSETS_KB = (0, 128, 384, 768, 1280, 1920, 2688)


def calculate_chunk_offset(chunk_id: int, size_multi: int = 1) -> int:
    """Byte offset at which the 1-indexed chunk `chunk_id` begins.

    The first 7 chunks use the fixed geometric offsets; from chunk 8 on they
    step by a flat 1MB (times `size_multi`, the multi-slot size factor).
    """
    if 1 <= chunk_id <= 7:
        return _GEOMETRIC_OFFSETS_KB[chunk_id - 1] * 1024
    return (3584 + (chunk_id - 8) * 1024 * size_multi) * 1024


def calculate_chunk_size(chunk_id: int, file_size: int, offset: int, size_multi: int = 1) -> int:
    """Length in bytes of chunk `chunk_id`, clamped so the final chunk stops
    exactly at `file_size` (the tail chunk is usually shorter than the rest)."""
    if 1 <= chunk_id <= 7:
        chunk_size = chunk_id * 128 * 1024
    else:
        chunk_size = 1024 * 1024 * size_multi

    if offset + chunk_size > file_size:
        chunk_size = file_size - offset
    return chunk_size


def gen_chunk_url(file_url: str, file_size: int, offset: int, chunk_size: int) -> str:
    """Build the storage-node URL for one chunk. MEGA addresses byte ranges by
    appending ``/offset-end`` to the file URL (its own convention, not an HTTP
    Range header); the final chunk uses the shorter ``/offset`` form."""
    if offset + chunk_size == file_size:
        return f"{file_url}/{offset}"
    return f"{file_url}/{offset}-{offset + chunk_size - 1}"


@dataclass(frozen=True)
class Chunk:
    """One chunk of a file: its 1-indexed id, byte `offset`, and `size`."""
    chunk_id: int
    offset: int
    size: int


def iter_chunks(file_size: int, size_multi: int = 1) -> Iterator[Chunk]:
    """Yield every Chunk (1-indexed) covering a file of `file_size` bytes."""
    chunk_id = 1
    while True:
        offset = calculate_chunk_offset(chunk_id, size_multi)
        if offset >= file_size:
            return
        size = calculate_chunk_size(chunk_id, file_size, offset, size_multi)
        if size <= 0:
            return
        yield Chunk(chunk_id=chunk_id, offset=offset, size=size)
        chunk_id += 1
