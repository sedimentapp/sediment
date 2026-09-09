import asyncio

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider

from sediment_mcp import server
from sediment_mcp.auth import StaticTokenVerifier
from sediment_mcp_ee_access.store import AccessStore
from sediment_mcp_ee_auth.github import AllowlistGitHubProvider


def test_existing_oauth_and_static_tokens_recheck_disabled_user(monkeypatch, tmp_path):
    store = AccessStore(str(tmp_path / "access.db"), create=True)
    doc = {"format_version": 1,
           "users": [{"principal": "alice", "github_id": "1001", "github_login": "old-login", "enabled": True}],
           "policy": {"grants": [{"users": ["alice"], "collections": ["acme"], "spaces": ["yt:ONE"]}]}}
    draft = store.save_draft(doc, author="admin", revision=0, base_version=1)
    store.apply(author="admin", revision=draft.revision, base_version=1, check_seats=lambda _: None)
    monkeypatch.setenv("MCP_ACCESS_MODE", "database")
    monkeypatch.setattr(server, "ACCESS", store)
    static = StaticTokenVerifier({"opaque-static": "alice"})
    github = object.__new__(AllowlistGitHubProvider)
    github._allowed_identities = None

    async def verified(self, token):
        return AccessToken(token=token, client_id="client", scopes=[], claims={"sub": "1001", "login": "renamed"})

    monkeypatch.setattr(GitHubProvider, "verify_token", verified)
    access = asyncio.run(github.verify_token("existing-oauth"))
    assert access is not None and access.claims is not None and access.claims["principal"] == "alice"
    assert asyncio.run(static.verify_token("opaque-static")) is not None
    doc["users"][0]["enabled"] = False
    draft = store.draft()
    saved = store.save_draft(doc, author="admin", revision=draft.revision, base_version=draft.base_version)
    store.apply(author="admin", revision=saved.revision, base_version=saved.base_version, check_seats=lambda _: None)
    assert asyncio.run(github.verify_token("existing-oauth")) is None
    assert asyncio.run(static.verify_token("opaque-static")) is None
