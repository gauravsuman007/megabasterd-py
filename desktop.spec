# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for the MegaBasterd-Py native desktop app.

Freezes ``desktop.py`` (the pywebview launcher) together with the FastAPI
application and its ``web/`` assets into a single self-contained bundle. Built
by ``.github/workflows/desktop-release.yml`` on every supported OS/arch:

    pyinstaller --noconfirm --clean desktop.spec

Why the explicit collection below:
  * ``web/``           -- Jinja2 templates + static JS/CSS are data files, not
                          importable modules; ``app.main`` resolves them relative
                          to ``__file__``, which lands under the bundle root, so
                          they must be shipped at ``web/``.
  * ``app`` submodules -- FastAPI routers are imported statically in
                          ``app.main``, but ``collect_submodules`` guarantees
                          nothing (e.g. a lazily-referenced module) is missed.
  * ``webview``        -- pywebview loads its platform backend and bundled JS
                          shim dynamically; ``collect_all`` grabs the backend
                          modules, their compiled bindings, and data files.
  * uvicorn hidden     -- uvicorn selects its event loop / HTTP / websocket
                          implementations by dynamic import at runtime; naming
                          them keeps the automatic ``auto`` selection working
                          (uvloop/httptools where built, asyncio/h11 elsewhere)
                          so there is no server-side performance regression.
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

app_name = "MegaBasterd"

# Architecture to freeze for. Left unset (None) on the native per-arch builds so
# PyInstaller targets the runner's own arch; the macOS universal build sets
# PYINSTALLER_TARGET_ARCH=universal2 so a single .app runs on both Intel and
# Apple Silicon. Requires every bundled binary to contain both slices, which the
# universal build guarantees by installing universal2 wheels.
target_arch = os.environ.get("PYINSTALLER_TARGET_ARCH") or None

datas = [("web", "web")]
binaries = []
hiddenimports = collect_submodules("app")

# pywebview's platform backend + assets.
_wv_datas, _wv_bins, _wv_hidden = collect_all("webview")
datas += _wv_datas
binaries += _wv_bins
hiddenimports += _wv_hidden

# uvicorn's dynamically-selected loop/protocol/lifespan implementations.
hiddenimports += [
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.loops.uvloop",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "httptools",
    "websockets",
    "wsproto",
    "h11",
    "anyio",
    "aiosqlite",
    "multipart",           # python-multipart (form parsing)
    "PIL",
]
# uvloop has no Windows wheel; guard so the spec builds identically everywhere.
try:
    import uvloop  # noqa: F401
    hiddenimports.append("uvloop")
except Exception:
    pass

# Templating engine data (Jinja2 has none, but python-multipart/anyio may).
datas += collect_data_files("certifi")

block_cipher = None

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PySide6", "PyQt5", "PyQt6"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=True,    # macOS: forward file-open events / proper argv
    target_arch=target_arch,  # None = runner's arch; "universal2" for the fat build
    codesign_identity=None,
    entitlements_file=None,
)

# macOS: wrap the executable in a proper .app bundle so it appears in the Dock
# with an icon and launches from Finder like a native application.
app_bundle = BUNDLE(
    exe,
    name=f"{app_name}.app",
    icon=None,
    bundle_identifier="com.gauravsuman.megabasterd",
    info_plist={
        "CFBundleName": app_name,
        "CFBundleDisplayName": app_name,
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
    },
)
