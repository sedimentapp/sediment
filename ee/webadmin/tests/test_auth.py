from types import SimpleNamespace
from typing import Any, cast

import pytest

from sediment_mcp_ee_webadmin.auth import (
    DevAdminAuth,
    GitHubAdminAuth,
    OAUTH_STATE_COOKIE,
    Signer,
    build_admin_auth,
)


def test_signer_roundtrip():
    signer = Signer("secret")
    assert signer.verify(signer.sign("alice", ttl=60)) == "alice"


def test_signer_rejects_tampering():
    signer = Signer("secret")
    token = signer.sign("alice", ttl=60)
    payload, expires, mac = token.split(".")
    forged_payload = "x" + payload[1:]
    assert signer.verify(f"{forged_payload}.{expires}.{mac}") is None
    assert signer.verify(f"{payload}.{int(expires) + 1}.{mac}") is None
    assert signer.verify("garbage") is None
    assert signer.verify("") is None


def test_signer_rejects_other_secret():
    token = Signer("secret-a").sign("alice", ttl=60)
    assert Signer("secret-b").verify(token) is None


def test_signer_rejects_expired():
    signer = Signer("secret")
    assert signer.verify(signer.sign("alice", ttl=-1)) is None


def _clear_admin_env(monkeypatch):
    for name in (
        "MCP_ADMIN_AUTH",
        "MCP_ADMIN_DEV_PRINCIPAL",
        "MCP_ADMIN_IDENTITIES",
        "MCP_ADMIN_SESSION_SECRET",
        "MCP_BASE_URL",
        "GITHUB_OAUTH_CLIENT_ID",
        "GITHUB_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_mode_is_required(monkeypatch):
    _clear_admin_env(monkeypatch)
    with pytest.raises(RuntimeError, match="MCP_ADMIN_AUTH"):
        build_admin_auth()


def test_dev_mode_requires_principal(monkeypatch):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    with pytest.raises(RuntimeError, match="MCP_ADMIN_DEV_PRINCIPAL"):
        build_admin_auth()


def test_dev_mode_fixed_lowercased_principal(monkeypatch):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "Alice")
    auth = build_admin_auth()
    assert isinstance(auth, DevAdminAuth)
    assert auth.principal(cast(Any, None)) == "alice"
    assert auth.login_routes is False


def test_github_mode_requires_full_env(monkeypatch):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("MCP_ADMIN_AUTH", "github")
    monkeypatch.setenv("MCP_ADMIN_IDENTITIES", "alice:10001")
    with pytest.raises(RuntimeError, match="MCP_BASE_URL"):
        build_admin_auth()


def _github_env(monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_AUTH", "github")
    monkeypatch.setenv("MCP_ADMIN_IDENTITIES", "alice:10001,other:42")
    monkeypatch.setenv("MCP_ADMIN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example/")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "csec")


def test_github_mode_builds(monkeypatch):
    _clear_admin_env(monkeypatch)
    _github_env(monkeypatch)
    auth = build_admin_auth()
    assert isinstance(auth, GitHubAdminAuth)
    assert auth.login_routes is True


def _cookie_request(cookies: dict[str, str]) -> Any:
    return SimpleNamespace(cookies=cookies)


def test_github_session_cookie(monkeypatch):
    _clear_admin_env(monkeypatch)
    _github_env(monkeypatch)
    auth = build_admin_auth()
    signer = Signer("test-secret")

    good = signer.sign("Alice", ttl=60)
    assert auth.principal(_cookie_request({"__Host-sediment_mcp_admin_session": good})) == "alice"

    not_allowed = signer.sign("mallory", ttl=60)
    assert auth.principal(_cookie_request({"__Host-sediment_mcp_admin_session": not_allowed})) is None

    forged = Signer("wrong").sign("alice", ttl=60)
    assert auth.principal(_cookie_request({"__Host-sediment_mcp_admin_session": forged})) is None

    assert auth.principal(_cookie_request({})) is None


def test_github_oauth_state_is_bound_to_browser_cookie(monkeypatch):
    _clear_admin_env(monkeypatch)
    _github_env(monkeypatch)
    auth = build_admin_auth()
    assert isinstance(auth, GitHubAdminAuth)
    signer = Signer("test-secret")
    nonce = "browser-nonce"
    state = signer.sign(f"login:{nonce}", ttl=60)
    cookie = signer.sign(nonce, ttl=60)
    request = SimpleNamespace(
        query_params={"state": state},
        cookies={OAUTH_STATE_COOKIE: cookie},
    )
    assert auth.valid_oauth_state(cast(Any, request))

    request.cookies[OAUTH_STATE_COOKIE] = signer.sign("other-browser", ttl=60)
    assert not auth.valid_oauth_state(cast(Any, request))
