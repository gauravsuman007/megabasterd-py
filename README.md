# MegaBasterd (Python port)

A Python/FastAPI port of [MegaBasterd](https://github.com/tonikelope/megabasterd), a MEGA.nz transfer client. Runs as a local web app instead of a Java/Swing desktop app: start it with `uvicorn`, open your browser.

Every crypto/protocol piece is ported directly from the original Java source (exact algorithms: key derivation, AES modes, MAC chaining, hashcash, chunk formulas) and verified against the real MEGA API, not just mocks.

The port tracks upstream release **v8.57**.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

## Running

```bash
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8009
```

Then open http://127.0.0.1:8009.

## Running with Docker

Prebuilt multi-architecture images (linux/amd64, linux/arm64, linux/arm/v7) are
published to Docker Hub at [`gauravsuman007/megabasterd-py`](https://hub.docker.com/r/gauravsuman007/megabasterd-py).
No build step needed — just pull and run:

```bash
docker run -d --name megabasterd -p 8009:8009 \
  -v megabasterd-data:/data \
  -v megabasterd-downloads:/downloads \
  gauravsuman007/megabasterd-py:latest
```

Then open http://127.0.0.1:8009. The account database (`/data`) and downloaded
files (`/downloads`) live in named Docker volumes, so they survive container
restarts and upgrades.

Or with Docker Compose (`docker-compose.yml` in this repo already points at the
published image):

```bash
docker compose up -d       # pull + start
docker compose down        # stop (keeps volumes/data)
docker compose down -v     # stop and wipe accounts/downloads too
```

### Image tags

| Tag | Contents |
|---|---|
| `latest`, `8.57` | Slim image (~250 MB), **no `ffmpeg`**. Image thumbnails (Pillow) work; video thumbnails are disabled. `latest` always tracks this slim build. |
| `8.57-thumbnails` | Adds `ffmpeg` for video thumbnails (~670 MB). Otherwise identical. |

To upgrade, pull the new tag and recreate the container — your volumes (accounts,
downloads) are preserved.

### Building from source (optional)

If you'd rather build locally instead of pulling:

```bash
docker build -t megabasterd-py .                                  # slim (no ffmpeg)
docker build --build-arg INCLUDE_FFMPEG=1 -t megabasterd-py:thumbnails .   # with ffmpeg
```

## Testing

```bash
./.venv/bin/pytest
```

## What's implemented

- **MEGA protocol/crypto** (`app/core/`): login (v1 legacy + v2 PBKDF2), node key derivation, AES-CBC/ECB/CTR, hashcash proof-of-work, RSA session-id decryption, link parsing (legacy `#!`/`#F!` and modern `/file/`/`/folder/` URLs).
- **Transfers** (`app/transfers/`): async parallel-chunk download and upload, with the exact chunk-size formula, incremental CBC-MAC verification, cancellation, and a per-direction concurrency cap (queued beyond that).
- **Accounts & persistence** (`app/storage/`): SQLite-backed account store with optional master-password-at-rest encryption (matches the original's storage format/PBKDF2 parameters).
- **SmartProxy** (`app/core/proxy_manager.py`): proxy pool parsing (inline entries + remote list URLs), rotation/banning, and automatic 509 (bandwidth quota) rerouting through both the API client and the chunk transfer layer. **You supply the proxy list yourself** (on the Settings page, one `ip:port` per line, `*ip:port` for SOCKS, or `#https://…` for a remote list) — this edition does not auto-discover or health-check free proxies.
- **Streaming proxy** (`app/streaming/`): Range-aware on-the-fly decryption for playing MEGA videos in a `<video>` tag without downloading the whole file first.
- **Auxiliary features** (`app/features/`): file split/merge, image/video thumbnailing (Pillow + ffmpeg), public-folder-link browsing and per-file download.
- **Web UI** (`web/`): accounts, downloads, uploads, folder browsing, SmartProxy settings, live progress over WebSocket.

## What's not implemented (by design, for now)

- Automatic free-proxy discovery / diagnostics (bring your own SmartProxy list).
- MegaCrypter (third-party link decryption service) support.
- i18n (the original ships 8 languages; this port is English-only).
- External command hooks (509-recovery script, post-queue-finish script).
- In-app debug log viewer (use the server's own stdout/stderr).
- Desktop-only concepts with no browser equivalent: system tray, JVM memory/GC display, OS-level clipboard spy (a background poll of the system clipboard isn't something a browser page can do — paste a link into the form instead).

## Project layout

```
app/
  core/        MEGA crypto, API client, link parsing, chunk math, proxy manager
  transfers/   async download/upload engines + CBC-MAC generator
  storage/     SQLite persistence + account encryption
  streaming/   Range-aware video proxy
  features/    file splitter/merger, thumbnailer, folder tree
  api/         FastAPI routers
  main.py      app entrypoint
web/           Jinja2 templates + static JS/CSS
tests/         pytest suite (crypto roundtrips, live-MEGA-verified algorithms, mocked transfer/proxy integration tests)
Dockerfile, docker-compose.yml   container image + local orchestration
```
