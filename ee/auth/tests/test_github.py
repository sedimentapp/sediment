import asyncio
from typing import Any

import pytest
from fastmcp.server.auth import AccessToken

import sediment_mcp_ee_auth.github as github_mod
from sediment_mcp_ee_auth.github import AllowlistGitHubProvider


def test_allowlist_provider_uses_immutable_id_and_stamps_principal(monkeypatch):
    async def verified(self, token):
        return AccessToken(
            token=token,
            client_id="10001",
            scopes=["user"],
            claims={"sub": "10001", "login": "renamed-user"},
        )

    monkeypatch.setattr(github_mod.GitHubProvider, "verify_token", verified)
    provider = object.__new__(AllowlistGitHubProvider)
    provider._allowed_identities = {"10001": "alice"}

    access = asyncio.run(provider.verify_token("token"))

    assert access is not None
    assert access.claims["principal"] == "alice"
    assert access.claims["login"] == "renamed-user"


def test_allowlist_provider_rejects_reused_login_with_other_id(monkeypatch):
    async def verified(self, token):
        return AccessToken(
            token=token,
            client_id="999",
            scopes=["user"],
            claims={"sub": "999", "login": "alice"},
        )

    monkeypatch.setattr(github_mod.GitHubProvider, "verify_token", verified)
    provider = object.__new__(AllowlistGitHubProvider)
    provider._allowed_identities = {"10001": "alice"}

    assert asyncio.run(provider.verify_token("token")) is None


def test_provider_passes_redirect_allowlist(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(github_mod, "require_ee", lambda feature: {})
    monkeypatch.setattr(github_mod, "AllowlistGitHubProvider", FakeProvider)
    monkeypatch.setattr(github_mod, "static_token_map", lambda: {})
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_GITHUB_ALLOWED_IDENTITIES", "alice:10001")
    monkeypatch.setenv(
        "MCP_ALLOWED_CLIENT_REDIRECT_URIS",
        "http://localhost:*,http://127.0.0.1:*",
    )

    result = github_mod.provider()

    assert isinstance(result, FakeProvider)
    assert captured["allowed_identities"] == {"10001": "alice"}
    assert captured["allowed_client_redirect_uris"] == [
        "http://localhost:*",
        "http://127.0.0.1:*",
    ]


def test_provider_requires_redirect_allowlist(monkeypatch):
    monkeypatch.setattr(github_mod, "require_ee", lambda feature: {})
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_GITHUB_ALLOWED_IDENTITIES", "alice:10001")
    monkeypatch.delenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)

    with pytest.raises(RuntimeError, match="MCP_ALLOWED_CLIENT_REDIRECT_URIS"):
        github_mod.provider()
