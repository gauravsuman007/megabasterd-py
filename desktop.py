"""Native-desktop launcher for the MegaBasterd-Py web app.

This is the entry point that PyInstaller freezes into the downloadable desktop
apps (see ``desktop.spec`` and ``.github/workflows/desktop-release.yml``). It is
a thin wrapper around the exact same FastAPI application served by ``uvicorn``
in the browser/Docker editions -- no application logic is duplicated here. It:

  1. Redirects the SQLite database and default download directory to writable,
     per-user locations (a frozen bundle's own directory is read-only, so the
     ``BASE_DIR``-relative defaults in ``app.state`` cannot be used).
  2. Widens ``PATH`` so an ``ffmpeg``/``ffprobe`` installed via Homebrew, the
     system package manager, or a standard Windows location is discoverable.
     A GUI app launched from Finder/Explorer inherits a minimal ``PATH`` that
     usually omits these, which would otherwise silently disable video
     thumbnails even when ffmpeg is installed.
  3. Starts ``uvicorn`` on a free localhost port in a background thread.
  4. Opens the app in the operating system's native webview (WebKit on macOS,
     WebView2 on Windows, WebKitGTK on Linux) via ``pywebview`` -- no bundled
     Chromium. Closing the window shuts the server down and exits.

This module lives only in the *public* edition (it is applied from
``public_overlay/``); the private/proxy edition does not ship desktop builds.
"""
from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading
import time
from pathlib import Path


def _user_data_dir() -> Path:
    """Return (creating if needed) the per-user directory that holds the app's
    database and, by default, its downloads.

    Uses the conventional per-OS location so data survives app upgrades and is
    never written inside the read-only application bundle:
      * Windows -> %APPDATA%\\MegaBasterd
      * macOS   -> ~/Library/Application Support/MegaBasterd
      * Linux   -> $XDG_DATA_HOME/MegaBasterd or ~/.local/share/MegaBasterd
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    data_dir = base / "MegaBasterd"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _augment_path_for_media_tools() -> None:
    """Prepend common ffmpeg/ffprobe install locations to ``PATH``.

    ``app.features.thumbnailer`` locates the tools with ``shutil.which`` and
    degrades gracefully when they are absent. A double-clicked GUI app, however,
    starts with a minimal ``PATH`` (e.g. ``/usr/bin:/bin`` on macOS) that omits
    Homebrew and other typical prefixes, so a perfectly-installed ffmpeg would
    look missing. Adding the standard directories here lets the *slim* build
    (which bundles no ffmpeg) use a system-installed one when present, without
    changing any shared application code. No-op on directories that don't exist.
    """
    candidates = [
        "/opt/homebrew/bin",   # macOS Apple-Silicon Homebrew
        "/usr/local/bin",      # macOS Intel Homebrew / common Unix
        "/usr/bin",
        "/bin",
        "/snap/bin",           # Linux snap-installed ffmpeg
        r"C:\ffmpeg\bin",      # common manual Windows install location
    ]
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    for path in candidates:
        if path not in parts and os.path.isdir(path):
            parts.append(path)
    os.environ["PATH"] = os.pathsep.join(parts)


def _free_port() -> int:
    """Return an OS-assigned free TCP port on the loopback interface.

    Bound and immediately released; the tiny race before uvicorn re-binds is
    acceptable for a single-user local app.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    """Configure the environment, start the server thread, and run the native
    window. Blocks until the user closes the window, then signals the server to
    stop. Side effects: creates the user-data dir, mutates ``os.environ``
    (``PATH``, ``MEGABASTERD_DB_PATH``, ``MEGABASTERD_DOWNLOAD_DIR``)."""
    multiprocessing.freeze_support()  # safe no-op unless a child process spawns
    _augment_path_for_media_tools()

    data_dir = _user_data_dir()
    # Only set these if the user hasn't overridden them, so power users can still
    # point the app elsewhere via the environment.
    os.environ.setdefault("MEGABASTERD_DB_PATH", str(data_dir / "megabasterd.db"))
    downloads = Path(os.environ.get("MEGABASTERD_DOWNLOAD_DIR", Path.home() / "Downloads" / "MegaBasterd"))
    downloads.mkdir(parents=True, exist_ok=True)
    os.environ["MEGABASTERD_DOWNLOAD_DIR"] = str(downloads)

    # Imported after the environment is set so state.py reads the right paths.
    import uvicorn
    import webview
    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    # uvicorn installs signal handlers only on the main thread; running serve()
    # off-thread simply skips them, which is what we want (the window owns exit).
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    # Wait for the server to accept connections before pointing the webview at
    # it, so the first paint isn't a connection-refused error.
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    webview.create_window(
        "MegaBasterd",
        f"http://127.0.0.1:{port}",
        width=1280,
        height=860,
        min_size=(900, 600),
    )
    try:
        webview.start()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
