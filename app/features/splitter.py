"""File split/merge, ported from FileSplitterDialog.java / FileMergerDialog.java.

Naming convention preserved exactly: ``<name>.part<i>-<total>`` (1-indexed).
Note: the Java merger discovers sibling parts with a plain lexicographic
``Collections.sort()``, which silently misorders once there are 10+ parts
(``part10`` sorts before ``part2``). This port extracts the numeric part
index and sorts on that instead -- same file format, correct ordering.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

PART_NAME_RE = re.compile(r"^(.+)\.part(\d+)-(\d+)$")

ProgressCallback = Callable[[int, int], None]
READ_BLOCK_SIZE = 1024 * 1024


def split_file(source_path: str, output_dir: str, mb_per_split: int, progress_cb: ProgressCallback | None = None) -> list[str]:
    if mb_per_split <= 0:
        raise ValueError("mb_per_split must be greater than zero")

    source = Path(source_path)
    size = source.stat().st_size
    bytes_per_split = 1024 * 1024 * mb_per_split
    num_full_parts, remaining_bytes = divmod(size, bytes_per_split)
    total_parts = num_full_parts + (1 if remaining_bytes else 0)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    progress = 0
    with open(source, "rb") as src:
        for i in range(1, total_parts + 1):
            part_size = bytes_per_split if i <= num_full_parts else remaining_bytes
            part_path = out_dir / f"{source.name}.part{i}-{total_parts}"
            with open(part_path, "wb") as dst:
                remaining_to_write = part_size
                while remaining_to_write > 0:
                    block = src.read(min(READ_BLOCK_SIZE, remaining_to_write))
                    if not block:
                        break
                    dst.write(block)
                    remaining_to_write -= len(block)
                    progress += len(block)
                    if progress_cb:
                        progress_cb(progress, size)
            parts.append(str(part_path))
    return parts


def discover_parts(any_part_path: str) -> tuple[str, list[str]]:
    """Given the path to any one ``.partN-M`` file, return
    ``(base_name, sorted_part_paths)`` for every sibling part in the same
    directory, ordered by numeric part index."""
    p = Path(any_part_path)
    m = PART_NAME_RE.match(p.name)
    if not m:
        raise ValueError(f"not a split-file part: {any_part_path}")

    base_name = m.group(1)
    directory = p.parent
    numbered: list[tuple[int, str]] = []
    for f in directory.iterdir():
        if not f.is_file():
            continue
        fm = PART_NAME_RE.match(f.name)
        if fm and fm.group(1) == base_name:
            numbered.append((int(fm.group(2)), str(f)))

    numbered.sort(key=lambda t: t[0])
    return base_name, [path for _, path in numbered]


def merge_parts(part_paths: list[str], dest_path: str, progress_cb: ProgressCallback | None = None, delete_parts_after: bool = False) -> None:
    total = sum(Path(p).stat().st_size for p in part_paths)
    progress = 0
    with open(dest_path, "wb") as dst:
        for part_path in part_paths:
            with open(part_path, "rb") as src:
                while True:
                    block = src.read(READ_BLOCK_SIZE)
                    if not block:
                        break
                    dst.write(block)
                    progress += len(block)
                    if progress_cb:
                        progress_cb(progress, total)

    if delete_parts_after:
        for part_path in part_paths:
            Path(part_path).unlink()
