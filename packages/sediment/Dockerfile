FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:e1869b1ddae289d6aa3c76649d0b663c11cb3215371146bf7dce95c821935fea

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/ packages/

RUN uv sync --frozen --package sediment --no-dev && \
    useradd --uid 1000 --no-create-home --shell /sbin/nologin sediment

USER 1000:1000
