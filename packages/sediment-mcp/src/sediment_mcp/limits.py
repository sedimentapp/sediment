"""Resource limits for authenticated MCP tool calls."""

import os
import time
from collections import defaultdict, deque

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

MAX_COLLECTION_CHARS = 128
MAX_QUERY_CHARS = 4_000
MAX_KEYWORDS = 20
MAX_KEYWORD_CHARS = 500
MAX_FILENAME_CHARS = 500
MAX_MANUAL_TEXT_CHARS = 24_000
MAX_TITLE_CHARS = 500
MAX_SEARCH_LIMIT = 100
RATE_WINDOW_SECONDS = 60.0


def rate_limit_per_minute() -> int:
    raw = os.environ.get("MCP_RATE_LIMIT_PER_MINUTE", "60")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"MCP_RATE_LIMIT_PER_MINUTE must be an integer, got {raw!r}") from exc
    if not 1 <= value <= 10_000:
        raise RuntimeError("MCP_RATE_LIMIT_PER_MINUTE must be between 1 and 10000")
    return value


class SlidingWindowRateLimiter:
    def __init__(self, calls_per_minute: int) -> None:
        self._limit = calls_per_minute
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, principal: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        calls = self._calls[principal]
        cutoff = current - RATE_WINDOW_SECONDS
        while calls and calls[0] <= cutoff:
            calls.popleft()
        if len(calls) >= self._limit:
            return False
        calls.append(current)
        return True


class RateLimitMiddleware(Middleware):
    def __init__(self, calls_per_minute: int) -> None:
        self._limiter = SlidingWindowRateLimiter(calls_per_minute)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        # Imported lazily to avoid an auth -> limits -> auth import cycle.
        from sediment_mcp.auth import current_principal

        principal = current_principal()
        if not self._limiter.allow(principal):
            # logged here since this runs outside the audit middleware
            logger.warning("Rate limit exceeded for principal %r", principal)
            raise ToolError("Rate limit exceeded. Retry in one minute.")
        return await call_next(context)
