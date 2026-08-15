import asyncio

import pytest
from fastmcp.server.auth import AccessToken

import sediment_mcp.auth as auth_mod
from sediment_mcp.auth import (
    STATIC_TOKEN_PREFIX,
    StaticTokenVerifier,
    current_principal,
    parse_github_identities,
    static_token_map,
)
from sediment_mcp.server import _manual_payload


def _clear_tokens(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("MCP_AUTH_TOKEN"):
            monkeypatch.delenv(key)


def test_named_tokens_map_to_lowercase_principals(monkeypatch):
    _clear_tokens(monkeypatch)
    monkeypatch.setenv(f"{STATIC_TOKEN_PREFIX}BOB", "secret-a")
    monkeypatch.setenv(f"{STATIC_TOKEN_PREFIX}Carol", "secret-b")
    assert static_token_map() == {"secret-a": "bob", "secret-b": "carol"}


def test_bare_token_rejected(monkeypatch):
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("MCP_AUTH_TOKEN", "legacy")
    with pytest.raises(RuntimeError, match="no longer supported"):
        static_token_map()


def test_duplicate_token_value_rejected(monkeypatch):
    _clear_tokens(monkeypatch)
    monkeypatch.setenv(f"{STATIC_TOKEN_PREFIX}A", "same")
    monkeypatch.setenv(f"{STATIC_TOKEN_PREFIX}B", "same")
    with pytest.raises(RuntimeError, match="ambiguous|Ambiguous"):
        static_token_map()


def test_empty_env_gives_empty_map(monkeypatch):
    _clear_tokens(monkeypatch)
    assert static_token_map() == {}


def test_empty_value_ignored(monkeypatch):
    _clear_tokens(monkeypatch)
    monkeypatch.setenv(f"{STATIC_TOKEN_PREFIX}BOB", "")
    assert static_token_map() == {}


def test_verify_token_returns_principal_as_client_id():
    verifier = StaticTokenVerifier({"secret-a": "bob", "secret-b": "carol"})
    access = asyncio.run(verifier.verify_token("secret-b"))
    assert access is not None
    assert access.client_id == "carol"


def test_verify_token_rejects_unknown():
    verifier = StaticTokenVerifier({"secret-a": "bob"})
    assert asyncio.run(verifier.verify_token("wrong")) is None


def _set_access(monkeypatch, access):
    monkeypatch.setattr(auth_mod, "get_access_token", lambda: access)


def test_current_principal_prefers_github_login(monkeypatch):
    _set_access(monkeypatch, AccessToken(
        token="t", client_id="oauth-client-uuid", scopes=[], claims={"login": "Alice"}))
    assert current_principal() == "alice"


def test_current_principal_prefers_stable_principal_claim(monkeypatch):
    _set_access(monkeypatch, AccessToken(
        token="t", client_id="10001", scopes=[],
        claims={"principal": "Alice", "login": "renamed-user", "sub": "10001"},
    ))
    assert current_principal() == "alice"


def test_current_principal_falls_back_to_client_id(monkeypatch):
    _set_access(monkeypatch, AccessToken(token="t", client_id="BOB", scopes=[]))
    assert current_principal() == "bob"


def test_current_principal_without_auth_context_fails(monkeypatch):
    _set_access(monkeypatch, None)
    with pytest.raises(RuntimeError, match="[Nn]o authenticated"):
        current_principal()


def test_manual_payload_is_server_stamped():
    payload = _manual_payload("carol", "note text", "notes/x", "", "owner")
    ts = payload.pop("ts")
    assert isinstance(ts, int) and ts > 0
    assert payload == {
        "text": "note text",
        "text_lc": "note text",
        "source": "manual",
        "file": "notes/x",
        "file_lc": "notes/x",
        "space": "manual:carol",
        "author": "carol",
        "visibility": "owner",
    }


def test_manual_payload_with_title():
    payload = _manual_payload("bob", "t", "f", "Title", "org")
    assert payload["title"] == "Title"
    assert payload["visibility"] == "org"


def test_parse_github_identities():
    assert parse_github_identities("Alice:10001, carol:42", "TEST") == {
        "10001": "alice",
        "42": "carol",
    }


@pytest.mark.parametrize("raw", ["alice", "alice:not-a-number", ":42", "user_name:42"])
def test_parse_github_identities_rejects_invalid_entries(raw):
    with pytest.raises(RuntimeError, match="principal:numeric_github_id"):
        parse_github_identities(raw, "TEST")


def test_parse_github_identities_rejects_ambiguous_mapping():
    with pytest.raises(RuntimeError, match="assigned to both"):
        parse_github_identities("one:42,two:42", "TEST")
