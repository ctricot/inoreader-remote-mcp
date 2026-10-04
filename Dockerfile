# syntax=docker/dockerfile:1
#
#     docker build -t inoreader-remote-mcp .

# --- 1. Build the virtual environment ------------------------------------------

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies before the code: this layer is only rebuilt when `uv.lock` changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
# `--no-editable`: the package is copied into the venv, not pointed at by a `.pth`.
RUN uv sync --frozen --no-dev --no-editable

# --- 2. Runtime: the venv alone, no uv, no sources ---------------------------------

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data

COPY --from=build /app/.venv /app/.venv

# UID 1000 / GID 100 (`users`): the files written to the mounted volume stay readable
# and writable from the host without sudo.
RUN useradd --uid 1000 --gid 100 --no-create-home --home-dir /app app \
    && mkdir -p /data \
    && chown 1000:100 /data

USER 1000:100
WORKDIR /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "inoreader_remote_mcp"]
