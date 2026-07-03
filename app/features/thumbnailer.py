"""Thumbnail generation, ported from Thumbnailer.java.

Images: resized to a 250px-tall PNG (matching IMAGE_THUMB_SIZE); if the
source is already shorter than that, the original path is returned
unchanged rather than upscaling. Videos: Java used Xuggle to grab a frame
at ~3% into the file; here that's delegated to `ffmpeg`/`ffprobe` (widely
available, no bundled codec library needed) and gracefully degrades to
None if they aren't installed rather than crashing the caller.
"""
from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

IMAGE_THUMB_SIZE = 250
VIDEO_FRAME_POSITION_PERCENT = 0.03
FFMPEG_TIMEOUT_SECS = 60
FFPROBE_TIMEOUT_SECS = 30


def is_video_file(filename: str) -> bool:
    """True if the filename's MIME type is a video type."""
    content_type, _ = mimetypes.guess_type(filename)
    return bool(content_type and content_type.startswith("video/"))


def is_image_file(filename: str) -> bool:
    """True if the filename's MIME type is an image type."""
    content_type, _ = mimetypes.guess_type(filename)
    return bool(content_type and content_type.startswith("image/"))


def create_image_thumbnail(filename: str) -> str:
    """Returns a new 250px-tall PNG's path, or `filename` unchanged if it's
    already short enough."""
    with Image.open(filename) as img:
        if img.height <= IMAGE_THUMB_SIZE:
            return filename
        height = IMAGE_THUMB_SIZE
        width = round(img.width * height / img.height)
        resized = img.convert("RGB").resize((width, height), Image.BICUBIC)

        fd, out_path = tempfile.mkstemp(prefix="megabasterd_thumbnail_", suffix=".png")
        os.close(fd)
        resized.save(out_path, "PNG")
        return out_path


def _ffprobe_duration_seconds(filename: str, ffprobe_path: str) -> float | None:
    """Video duration in seconds via ffprobe, or None if it fails/isn't parseable."""
    try:
        result = subprocess.run(
            [ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filename],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECS,
        )
        return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def create_video_thumbnail(filename: str, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> str | None:
    """Returns a JPEG thumbnail path grabbed at ~3% into the video, or None
    if ffmpeg/ffprobe aren't available or extraction fails -- never raises,
    matching the Java version's "no thumbnail is fine" fallback."""
    if shutil.which(ffmpeg_path) is None or shutil.which(ffprobe_path) is None:
        return None

    duration = _ffprobe_duration_seconds(filename, ffprobe_path)
    if duration is None or duration <= 0:
        return None

    seek_seconds = duration * VIDEO_FRAME_POSITION_PERCENT
    fd, out_path = tempfile.mkstemp(prefix="megabasterd_thumbnail_", suffix=".jpg")
    os.close(fd)

    try:
        proc = subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-ss", str(seek_seconds),
                "-i", filename,
                "-frames:v", "1",
                "-vf", f"scale=-2:{IMAGE_THUMB_SIZE}",
                out_path,
            ],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECS,
        )
    except subprocess.SubprocessError:
        Path(out_path).unlink(missing_ok=True)
        return None

    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        Path(out_path).unlink(missing_ok=True)
        return None
    return out_path


def create_thumbnail(filename: str) -> str | None:
    """Dispatch to the video or image thumbnailer by file type; None for
    anything else (or when a thumbnail can't be produced)."""
    if is_video_file(filename):
        return create_video_thumbnail(filename)
    if is_image_file(filename):
        return create_image_thumbnail(filename)
    return None
