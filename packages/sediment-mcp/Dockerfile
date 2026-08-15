FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:e1869b1ddae289d6aa3c76649d0b663c11cb3215371146bf7dce95c821935fea

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/home/sediment-mcp \
    TMPDIR=/tmp \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY ee/ ee/

# one sync per ee package (uv sync takes a single --package);
# --inexact keeps the previously installed one in place
RUN uv sync --frozen --package sediment-mcp-ee-auth --no-dev && \
    uv sync --frozen --package sediment-mcp-ee-webadmin --no-dev --inexact && \
    useradd --uid 1000 --create-home --home-dir /home/sediment-mcp \
      --shell /usr/sbin/nologin sediment-mcp

EXPOSE 8080
USER 1000:1000
CMD ["sediment-mcp-serve"]
