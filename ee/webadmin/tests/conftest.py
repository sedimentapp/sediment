import os
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# sediment_mcp.server requires these at import time; set before any test imports it.
# QdrantClient does not connect eagerly, so a dummy URL is fine for unit tests.
os.environ.setdefault("QDRANT_URL", "http://qdrant.invalid:6333")
os.environ.setdefault("EMBED_URL", "http://embed.invalid:8080")
os.environ.setdefault("EMBED_MODEL", "test-model")
os.environ.setdefault("MCP_ACL_DISABLE", "1")

# register() requires a valid ee license; sign one with a throwaway key
# and pin its public key for every test.
_LICENSE_KEY = Ed25519PrivateKey.generate()
_LICENSE_TOKEN = jwt.encode(
    {"sub": "test", "iat": int(time.time()), "exp": int(time.time()) + 3600, "seats": 100},
    _LICENSE_KEY,
    algorithm="EdDSA",
)


@pytest.fixture(autouse=True)
def ee_license(monkeypatch):
    import sediment_mcp_ee_license as lic

    monkeypatch.setattr(
        lic, "PUBLIC_KEY_HEX", [_LICENSE_KEY.public_key().public_bytes_raw().hex()]
    )
    monkeypatch.setattr(lic, "_cached_claims", None)
    monkeypatch.setenv("MCP_LICENSE", _LICENSE_TOKEN)
