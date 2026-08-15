"""License verification tests: throwaway Ed25519 keypair pinned via monkeypatch."""

import os
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import sediment_mcp_ee_license as lic

SIGNING_KEY = Ed25519PrivateKey.generate()
FOREIGN_KEY = Ed25519PrivateKey.generate()


def make_token(key=SIGNING_KEY, *, sub="test-licensee", ttl=3600, seats: object = 10, drop=()):
    now = int(time.time())
    claims = {"sub": sub, "iat": now, "exp": now + ttl, "seats": seats}
    for name in drop:
        del claims[name]
    return jwt.encode(claims, key, algorithm="EdDSA")


@pytest.fixture(autouse=True)
def pinned_key(monkeypatch):
    monkeypatch.setattr(
        lic, "PUBLIC_KEY_HEX", [SIGNING_KEY.public_key().public_bytes_raw().hex()]
    )
    monkeypatch.setattr(lic, "_cached_claims", None)
    monkeypatch.delenv("MCP_LICENSE", raising=False)
    monkeypatch.delenv("MCP_LICENSE_FILE", raising=False)
    # seat accounting must see only what each test configures
    for key in [k for k in os.environ if k.startswith("MCP_AUTH_TOKEN_")]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("MCP_GITHUB_ALLOWED_IDENTITIES", raising=False)
    monkeypatch.delenv("MCP_ADMIN_IDENTITIES", raising=False)
    monkeypatch.delenv("MCP_ADMIN_DEV_PRINCIPAL", raising=False)


def test_valid_license(monkeypatch):
    monkeypatch.setenv("MCP_LICENSE", make_token())
    claims = lic.require_ee("webadmin")
    assert claims["sub"] == "test-licensee"


def test_license_from_file(monkeypatch, tmp_path):
    license_file = tmp_path / "license.jwt"
    license_file.write_text(make_token() + "\n")
    monkeypatch.setenv("MCP_LICENSE_FILE", str(license_file))
    assert lic.require_ee("webadmin")["sub"] == "test-licensee"


def test_missing_license(monkeypatch):
    with pytest.raises(RuntimeError, match="no license configured"):
        lic.require_ee("webadmin")


def test_both_sources_set(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_LICENSE", make_token())
    monkeypatch.setenv("MCP_LICENSE_FILE", str(tmp_path / "license.jwt"))
    with pytest.raises(RuntimeError, match="ambiguous"):
        lic.require_ee("webadmin")


def test_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_LICENSE_FILE", str(tmp_path / "nope.jwt"))
    with pytest.raises(RuntimeError, match="file not found"):
        lic.require_ee("webadmin")


def test_expired_license(monkeypatch):
    monkeypatch.setenv("MCP_LICENSE", make_token(ttl=-3600))
    with pytest.raises(RuntimeError, match="expired"):
        lic.require_ee("webadmin")


def test_foreign_signature(monkeypatch):
    monkeypatch.setenv("MCP_LICENSE", make_token(FOREIGN_KEY))
    with pytest.raises(RuntimeError, match="does not match any known signing key"):
        lic.require_ee("webadmin")


def test_garbage_token(monkeypatch):
    monkeypatch.setenv("MCP_LICENSE", "not-a-jwt")
    with pytest.raises(RuntimeError, match="invalid license"):
        lic.require_ee("webadmin")


def test_missing_required_claim(monkeypatch):
    monkeypatch.setenv("MCP_LICENSE", make_token(drop=["exp"]))
    with pytest.raises(RuntimeError, match="invalid license"):
        lic.require_ee("webadmin")


def test_missing_seats_claim(monkeypatch):
    monkeypatch.setenv("MCP_LICENSE", make_token(drop=["seats"]))
    with pytest.raises(RuntimeError, match="invalid license"):
        lic.require_ee("webadmin")


def test_seats_not_an_int(monkeypatch):
    monkeypatch.setenv("MCP_LICENSE", make_token(seats="10"))
    with pytest.raises(RuntimeError, match="seats must be a positive integer"):
        lic.require_ee("webadmin")


def test_seats_exceeded(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN_ALICE", "tok-a")
    monkeypatch.setenv("MCP_GITHUB_ALLOWED_IDENTITIES", "dave:1,carol:2")
    monkeypatch.setenv("MCP_LICENSE", make_token(seats=2))
    with pytest.raises(RuntimeError, match="allows 2 seat.s. but 3 principals"):
        lic.require_ee("webadmin")


def test_seats_exact_fit(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN_ALICE", "tok-a")
    monkeypatch.setenv("MCP_GITHUB_ALLOWED_IDENTITIES", "dave:1")
    monkeypatch.setenv("MCP_LICENSE", make_token(seats=2))
    assert lic.require_ee("webadmin")["seats"] == 2


def test_same_principal_via_several_paths_is_one_seat(monkeypatch):
    """Static token BOB + github allowlist bob + admin bob = the same human."""
    monkeypatch.setenv("MCP_AUTH_TOKEN_BOB", "tok-d")
    monkeypatch.setenv("MCP_GITHUB_ALLOWED_IDENTITIES", "bob:1")
    monkeypatch.setenv("MCP_ADMIN_IDENTITIES", "BOB:1")
    monkeypatch.setenv("MCP_LICENSE", make_token(seats=1))
    assert lic.require_ee("webadmin")["seats"] == 1


def test_dev_admin_principal_uses_a_seat(monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "Developer")
    monkeypatch.setenv("MCP_LICENSE", make_token(seats=1))
    assert lic.require_ee("webadmin")["seats"] == 1


def test_key_rotation(monkeypatch):
    """A license verifies if ANY pinned key matches, not just the first."""
    monkeypatch.setattr(
        lic,
        "PUBLIC_KEY_HEX",
        [
            FOREIGN_KEY.public_key().public_bytes_raw().hex(),
            SIGNING_KEY.public_key().public_bytes_raw().hex(),
        ],
    )
    monkeypatch.setenv("MCP_LICENSE", make_token())
    assert lic.require_ee("webadmin")["sub"] == "test-licensee"


def test_claims_cached(monkeypatch):
    monkeypatch.setenv("MCP_LICENSE", make_token())
    first = lic.require_ee("webadmin")
    monkeypatch.delenv("MCP_LICENSE")
    assert lic.require_ee("auth-github") is first
