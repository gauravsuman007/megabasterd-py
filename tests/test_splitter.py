import os

from app.features.splitter import discover_parts, merge_parts, split_file


def test_split_exact_multiple_of_split_size(tmp_path):
    src = tmp_path / "source.bin"
    data = os.urandom(3 * 1024 * 1024)  # exactly 3 x 1MB
    src.write_bytes(data)

    out_dir = tmp_path / "parts"
    parts = split_file(str(src), str(out_dir), mb_per_split=1)

    assert len(parts) == 3
    assert [os.path.basename(p) for p in parts] == [
        "source.bin.part1-3",
        "source.bin.part2-3",
        "source.bin.part3-3",
    ]
    for i, part in enumerate(parts):
        assert (out_dir / os.path.basename(part)).stat().st_size == 1024 * 1024
        assert (out_dir / os.path.basename(part)).read_bytes() == data[i * 1024 * 1024 : (i + 1) * 1024 * 1024]


def test_split_with_remainder(tmp_path):
    src = tmp_path / "source.bin"
    data = os.urandom(2 * 1024 * 1024 + 500)
    src.write_bytes(data)

    out_dir = tmp_path / "parts"
    parts = split_file(str(src), str(out_dir), mb_per_split=1)

    assert len(parts) == 3
    assert os.path.basename(parts[-1]) == "source.bin.part3-3"
    assert (out_dir / "source.bin.part3-3").stat().st_size == 500


def test_split_rejects_non_positive_size(tmp_path):
    src = tmp_path / "source.bin"
    src.write_bytes(b"data")
    import pytest

    with pytest.raises(ValueError):
        split_file(str(src), str(tmp_path / "out"), mb_per_split=0)


def test_split_then_merge_roundtrip(tmp_path):
    src = tmp_path / "source.bin"
    data = os.urandom(5 * 1024 * 1024 + 12345)
    src.write_bytes(data)

    out_dir = tmp_path / "parts"
    progress_events = []
    parts = split_file(str(src), str(out_dir), mb_per_split=1, progress_cb=lambda done, total: progress_events.append((done, total)))

    assert progress_events[-1] == (len(data), len(data))

    base_name, discovered = discover_parts(parts[0])
    assert base_name == "source.bin"
    assert discovered == parts

    dest = tmp_path / "merged.bin"
    merge_parts(discovered, str(dest))
    assert dest.read_bytes() == data


def test_discover_parts_sorts_numerically_not_lexicographically(tmp_path):
    # 11 parts so a plain string sort (Java's Collections.sort) would put
    # "part10" before "part2" -- this must sort 1..11 numerically instead.
    base = "movie.mkv"
    for i in range(1, 12):
        (tmp_path / f"{base}.part{i}-11").write_bytes(bytes([i]))

    base_name, parts = discover_parts(str(tmp_path / f"{base}.part1-11"))
    assert base_name == base
    assert [os.path.basename(p) for p in parts] == [f"{base}.part{i}-11" for i in range(1, 12)]


def test_discover_parts_ignores_unrelated_files_with_similar_prefix(tmp_path):
    (tmp_path / "movie.mkv.part1-2").write_bytes(b"a")
    (tmp_path / "movie.mkv.part2-2").write_bytes(b"b")
    (tmp_path / "movie.mkv.something.part1-2").write_bytes(b"x")  # different base name
    (tmp_path / "movie.mkv.txt").write_bytes(b"y")

    base_name, parts = discover_parts(str(tmp_path / "movie.mkv.part1-2"))
    assert base_name == "movie.mkv"
    assert len(parts) == 2


def test_merge_can_delete_parts_after(tmp_path):
    out_dir = tmp_path / "parts"
    src = tmp_path / "source.bin"
    src.write_bytes(os.urandom(1024))
    parts = split_file(str(src), str(out_dir), mb_per_split=1)

    dest = tmp_path / "merged.bin"
    merge_parts(parts, str(dest), delete_parts_after=True)

    assert dest.read_bytes() == src.read_bytes()
    for p in parts:
        assert not os.path.exists(p)
