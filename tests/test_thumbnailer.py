import shutil
import subprocess

import pytest
from PIL import Image

from app.features import thumbnailer


def test_is_video_file_and_is_image_file():
    assert thumbnailer.is_video_file("movie.mp4") is True
    assert thumbnailer.is_video_file("photo.jpg") is False
    assert thumbnailer.is_image_file("photo.jpg") is True
    assert thumbnailer.is_image_file("movie.mp4") is False
    assert thumbnailer.is_video_file("archive.zip") is False
    assert thumbnailer.is_image_file("archive.zip") is False


def test_image_thumbnail_resizes_tall_image(tmp_path):
    src = tmp_path / "tall.png"
    Image.new("RGB", (200, 1000), color=(10, 20, 30)).save(src)

    out = thumbnailer.create_image_thumbnail(str(src))
    assert out != str(src)

    with Image.open(out) as thumb:
        assert thumb.height == thumbnailer.IMAGE_THUMB_SIZE
        assert thumb.width == round(200 * 250 / 1000)


def test_image_thumbnail_returns_original_when_already_small(tmp_path):
    src = tmp_path / "small.png"
    Image.new("RGB", (80, 60), color=(1, 2, 3)).save(src)

    out = thumbnailer.create_image_thumbnail(str(src))
    assert out == str(src)


def test_image_thumbnail_preserves_aspect_ratio_for_wide_image(tmp_path):
    src = tmp_path / "wide.png"
    Image.new("RGB", (2000, 500), color=(5, 5, 5)).save(src)

    out = thumbnailer.create_image_thumbnail(str(src))
    with Image.open(out) as thumb:
        assert thumb.height == 250
        assert thumb.width == round(2000 * 250 / 500)


def test_create_thumbnail_dispatches_by_content_type(tmp_path):
    src = tmp_path / "img.png"
    Image.new("RGB", (10, 400)).save(src)
    result = thumbnailer.create_thumbnail(str(src))
    assert result is not None

    other = tmp_path / "notes.txt"
    other.write_text("hello")
    assert thumbnailer.create_thumbnail(str(other)) is None


def test_video_thumbnail_gracefully_degrades_when_tools_missing(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"not a real video")
    result = thumbnailer.create_video_thumbnail(str(fake_video), ffmpeg_path="no-such-ffmpeg-binary", ffprobe_path="no-such-ffprobe-binary")
    assert result is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not installed")
def test_video_thumbnail_extracts_real_frame(tmp_path):
    src = tmp_path / "synthetic.mp4"
    # Generate a tiny 2-second synthetic test video with ffmpeg itself.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-pix_fmt", "yuv420p",
            str(src),
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )

    out = thumbnailer.create_video_thumbnail(str(src))
    assert out is not None
    with Image.open(out) as thumb:
        assert thumb.height == thumbnailer.IMAGE_THUMB_SIZE
        assert thumb.format == "JPEG"
