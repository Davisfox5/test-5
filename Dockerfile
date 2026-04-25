# syntax=docker/dockerfile:1.7
#
# LINDA backend image.
#
# One image, three processes: api / worker / beat. Process selection is
# done in fly.toml's [processes] block — the default CMD here boots the
# API so `docker run` produces a working server.
#
# Heavy ML deps (torch, pyannote, speechbrain) are installed against the
# CPU-only torch wheel index to keep the image around 1.2 GB instead of
# 5 GB+ (CUDA wheels). Fly's shared-cpu hardware has no GPU anyway.

ARG PYTHON_VERSION=3.12

# ─── Builder ────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        git \
        libsndfile1-dev \
        ffmpeg \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt ./

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel setuptools \
    && /opt/venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu \
           "torch>=2.1" "torchaudio>=2.1" \
    && /opt/venv/bin/pip install -r requirements.txt

# ─── Runtime ────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        libgomp1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 linda

COPY --from=builder /opt/venv /opt/venv

WORKDIR /home/linda/app
COPY --chown=linda:linda . .

USER linda

# Make `backend.app.*` importable when uvicorn / celery / alembic launch
# as console-script entrypoints — their sys.path does not include CWD.
ENV PYTHONPATH=/home/linda/app

ARG RELEASE_VERSION=dev
ENV RELEASE_VERSION=${RELEASE_VERSION}

EXPOSE 8000

# Default process = API. Workers/beat override this via fly.toml [processes].
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
