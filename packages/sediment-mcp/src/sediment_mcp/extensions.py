"""Extension loading.

Extension packages (ee/*) register additional functionality on the FastMCP
app — typically custom Starlette routes — via the "sediment_mcp.extensions"
entry-point group. Each entry point is a callable `register(mcp) -> None`
that reads its own configuration from the environment and fails fast when
it is incomplete.

MCP_EXTENSIONS is a comma-separated list of extension names to load;
unset or empty means no extensions. An unknown name is a fatal startup
error — a typo must not silently disable a feature.
"""

import os
from importlib.metadata import entry_points

from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger

ENTRY_POINT_GROUP = "sediment_mcp.extensions"

logger = get_logger(__name__)


def load_extensions(mcp: FastMCP) -> None:
    raw = os.environ.get("MCP_EXTENSIONS", "")
    for name in [n.strip() for n in raw.split(",") if n.strip()]:
        matches = entry_points(group=ENTRY_POINT_GROUP, name=name)
        if not matches:
            available = sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))
            raise RuntimeError(
                f"Unknown extension {name!r} (MCP_EXTENSIONS). "
                f"Available: {', '.join(available) or 'none installed'}"
            )
        entry_point = next(iter(matches))
        register = entry_point.load()
        register(mcp)
        logger.info("Loaded extension %r from %s", name, entry_point.value)
