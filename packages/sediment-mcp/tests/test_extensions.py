from typing import Any, cast

import pytest

from sediment_mcp.extensions import load_extensions

# the loader only forwards mcp to extension factories; no FastMCP needed here
_MCP_STUB = cast(Any, object())


def test_no_env_loads_nothing(monkeypatch):
    monkeypatch.delenv("MCP_EXTENSIONS", raising=False)
    load_extensions(_MCP_STUB)  # must not raise, must not call anything


def test_unknown_extension_is_fatal(monkeypatch):
    monkeypatch.setenv("MCP_EXTENSIONS", "no-such-extension")
    with pytest.raises(RuntimeError, match="no-such-extension"):
        load_extensions(_MCP_STUB)
