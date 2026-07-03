from app.core.chunks import calculate_chunk_offset, calculate_chunk_size, gen_chunk_url, iter_chunks


def test_geometric_offsets_and_sizes():
    expected_offsets_kb = [0, 128, 384, 768, 1280, 1920, 2688]
    for i, off_kb in enumerate(expected_offsets_kb, start=1):
        offset = calculate_chunk_offset(i, size_multi=1)
        assert offset == off_kb * 1024
        size = calculate_chunk_size(i, file_size=10**9, offset=offset, size_multi=1)
        assert size == i * 128 * 1024


def test_fixed_size_phase_after_chunk_7():
    offset8 = calculate_chunk_offset(8, size_multi=1)
    assert offset8 == 3584 * 1024
    size8 = calculate_chunk_size(8, file_size=10**9, offset=offset8, size_multi=1)
    assert size8 == 1024 * 1024

    offset9 = calculate_chunk_offset(9, size_multi=1)
    assert offset9 == (3584 + 1024) * 1024


def test_size_multi_scales_fixed_phase_only():
    # size_multi must not affect the first 7 geometric chunks, only chunk 8+.
    for i in range(1, 8):
        assert calculate_chunk_offset(i, size_multi=20) == calculate_chunk_offset(i, size_multi=1)
    offset8_multi = calculate_chunk_offset(8, size_multi=20)
    assert offset8_multi == 3584 * 1024
    offset9_multi = calculate_chunk_offset(9, size_multi=20)
    assert offset9_multi == (3584 + 1024 * 20) * 1024


def test_last_chunk_truncated_to_file_size():
    file_size = 3584 * 1024 + 500  # a bit into chunk 8
    offset8 = calculate_chunk_offset(8, size_multi=1)
    size8 = calculate_chunk_size(8, file_size, offset8, size_multi=1)
    assert size8 == 500


def test_gen_chunk_url_omits_end_at_eof():
    assert gen_chunk_url("http://x/y", file_size=1000, offset=900, chunk_size=100) == "http://x/y/900"
    assert gen_chunk_url("http://x/y", file_size=1000, offset=0, chunk_size=500) == "http://x/y/0-499"


def test_iter_chunks_covers_whole_file_exactly():
    for file_size in (1, 500, 128 * 1024, 3584 * 1024 + 12345, 10 * 1024 * 1024 + 7):
        chunks = list(iter_chunks(file_size, size_multi=1))
        assert chunks[0].offset == 0
        total = sum(c.size for c in chunks)
        assert total == file_size
        # contiguous, no gaps/overlaps
        expected_offset = 0
        for c in chunks:
            assert c.offset == expected_offset
            expected_offset += c.size
        # ids are sequential starting at 1
        assert [c.chunk_id for c in chunks] == list(range(1, len(chunks) + 1))


def test_iter_chunks_with_size_multi_20_matches_real_download_first_chunk():
    # Regression pin: the live MEGA smoke test downloaded chunk 1 as exactly
    # 128KB, matching the geometric formula regardless of size_multi.
    chunks = list(iter_chunks(1791516258, size_multi=20))
    assert chunks[0].size == 128 * 1024
    assert chunks[0].offset == 0
