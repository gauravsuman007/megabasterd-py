FROM python:3.12-slim

# Video thumbnails need ffmpeg; app/features/thumbnailer.py degrades gracefully
# without it (image thumbnails via Pillow still work). ffmpeg is ~420 MB, so the
# default image omits it (~250 MB); build the :<version>-thumbnails variant to
# include it:
#   docker build --build-arg INCLUDE_FFMPEG=1 -t megabasterd-py:thumbnails .
ARG INCLUDE_FFMPEG=0

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY web ./web

# Install the Python dependencies.
#
# On linux/amd64 and linux/arm64 these all resolve to prebuilt manylinux wheels,
# so no compiler is involved. On the 32-bit targets (linux/arm/v7, and linux/386
# for some packages) several of them (pillow, pycryptodome, httptools, uvloop)
# publish no wheel and must be built from source, which needs a C toolchain plus
# the JPEG/zlib headers Pillow links against. We install that toolchain
# unconditionally (harmless where wheels already exist), build, and then purge it
# in the SAME layer,
# so the runtime image never ships a compiler -- only the small runtime shared
# libraries Pillow loads at run time (libjpeg, zlib), plus ffmpeg when requested,
# remain. The default image therefore stays slim on every architecture.
RUN set -eux; \
    apt-get update; \
    BUILD_DEPS="build-essential autoconf automake libtool pkg-config python3-dev libjpeg-dev zlib1g-dev"; \
    apt-get install -y --no-install-recommends $BUILD_DEPS libjpeg62-turbo zlib1g; \
    if [ "$INCLUDE_FFMPEG" = "1" ]; then apt-get install -y --no-install-recommends ffmpeg; fi; \
    pip install --no-cache-dir .; \
    apt-get purge -y --auto-remove $BUILD_DEPS; \
    rm -rf /var/lib/apt/lists/*

# Persisted across restarts via the compose volumes below.
ENV MEGABASTERD_DB_PATH=/data/megabasterd.db
ENV MEGABASTERD_DOWNLOAD_DIR=/downloads
RUN mkdir -p /data /downloads

EXPOSE 8009

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8009"]
