FROM python:3.12-slim

# ffmpeg is optional at runtime (app/features/thumbnailer.py degrades
# gracefully without it) but bundled here so video thumbnailing works
# out of the box.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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
