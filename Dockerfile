# syntax=docker/dockerfile:1

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps: ffmpeg for all media work, cairo/pango for Manim,
# espeak-ng as the offline TTS fallback, fonts for subtitle and card rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        pkg-config cmake build-essential \
        libcairo2-dev libpango1.0-dev \
        espeak-ng \
        fonts-dejavu-core fonts-noto-color-emoji \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers for website QA (chromium only, keeps the image smaller)
RUN playwright install --with-deps chromium

COPY . .

RUN mkdir -p /app/data /app/outputs

# Non-root runtime user
RUN useradd --create-home --shell /bin/bash agent \
    && chown -R agent:agent /app
USER agent

ENV OUTPUTS_DIR=/app/outputs \
    DATA_DIR=/app/data \
    MEMORY_DIR=/app/data/memory

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

CMD ["python", "main.py"]
