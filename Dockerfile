# syntax=docker/dockerfile:1.7
#
# One image, two entrypoints. The API and the worker run the same code and
# differ only in the command, which is what keeps "it worked in the API" and
# "it worked in the worker" from becoming different statements.
#
# Multi-stage so the runtime image carries no build toolchain: a smaller attack
# surface, and a smaller thing to pull on every deploy.

# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependencies first, so a source change does not invalidate the layer that
# takes the longest to build.
COPY pyproject.toml README.md ./
# Exact version pins, tested. NOT hash-pinned: see `requirements.lock.md` for
# why (no package index is reachable from this build environment) and for the
# two-line change that closes it. Claiming `--require-hashes` against digests
# nobody verified would be worse than saying so.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
RUN /opt/venv/bin/pip install --no-deps .

# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# ffmpeg is a hard runtime dependency of the renderer and of audio probing.
# fonts-noto covers the scripts `vtv.animation.fonts` reports on; without them
# the multilingual paths degrade honestly but visibly.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      ffmpeg \
      fonts-dejavu-core \
      fonts-noto-core \
      fonts-noto-cjk \
      fonts-noto-color-emoji \
      libraqm0 \
 && rm -rf /var/lib/apt/lists/*

# Never root. A parser exploit inside a malicious document should not land on a
# privileged process.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin vtv

COPY --from=build /opt/venv /opt/venv
COPY apps/ /srv/vtv/apps/
COPY migrations/ /srv/vtv/migrations/
COPY schemas/ /srv/vtv/schemas/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VTV_ENV=production \
    VTV_STORAGE_ROOT=/var/lib/vtv/storage

WORKDIR /srv/vtv
RUN mkdir -p /var/lib/vtv/storage && chown -R vtv:vtv /var/lib/vtv /srv/vtv
USER vtv

EXPOSE 8000

# Liveness only — "is this process able to answer at all". Readiness is a
# separate endpoint that checks dependencies, because a process that is alive
# but cannot reach its database should stop receiving traffic without being
# killed and restarted.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status==200 else 1)"

# Default to the API. `command: ["python","-m","vtv.worker"]` selects the other.
CMD ["uvicorn", "vtv.api.app:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
