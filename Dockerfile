FROM python:3.12-slim

# ffmpeg is only used for *video* thumbnails; app/features/thumbnailer.py
# degrades gracefully without it (image thumbnails via Pillow still work).
# It weighs ~420 MB, so the default image omits it (~250 MB total). To build
# the video-thumbnail variant instead (~670 MB):
#   docker build --build-arg INCLUDE_FFMPEG=1 -t megabasterd-py:thumbnails .
ARG INCLUDE_FFMPEG=0
RUN if [ "$INCLUDE_FFMPEG" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends ffmpeg \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY web ./web

RUN pip install --no-cache-dir .

# Persisted across restarts via the compose volumes below.
ENV MEGABASTERD_DB_PATH=/data/megabasterd.db
ENV MEGABASTERD_DOWNLOAD_DIR=/downloads
RUN mkdir -p /data /downloads

EXPOSE 8009

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8009"]
