#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyjwt[crypto]>=2"]
# ///
"""Offline issuing of ee licenses for sediment-mcp.

Dev tooling only — never ships in the image; the private key never leaves
the issuing machine.

    uv run tools/license_issue.py keygen
        Generate an Ed25519 signing keypair. Private key (PEM, 0600) goes
        to --out (default ~/.config/sediment/license-signing.key); the
        public key hex is printed — pin it in ee/license PUBLIC_KEY_HEX.

    uv run tools/license_issue.py issue --sub <licensee> --seats <n> [--days 365]
        Sign a flat ee license JWT and print it. Seats cap the number of
        distinct configured principals (static tokens + allowlists).
        Deliver via the MCP_LICENSE env var (k8s: SOPS secret) or a
        file + MCP_LICENSE_FILE.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_KEY_PATH = Path.home() / ".config" / "sediment" / "license-signing.key"


def keygen(args: argparse.Namespace) -> None:
    out: Path = args.out
    if out.exists():
        sys.exit(f"{out} already exists — refusing to overwrite a signing key")
    key = Ed25519PrivateKey.generate()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.touch(mode=0o600)
    out.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    print(f"private key: {out}", file=sys.stderr)
    print(key.public_key().public_bytes_raw().hex())


def issue(args: argparse.Namespace) -> None:
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit(f"{args.key}: not an Ed25519 private key")
    if args.seats < 1:
        sys.exit("--seats must be a positive integer")
    now = datetime.now(UTC)
    claims = {
        "sub": args.sub,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=args.days)).timestamp()),
        "seats": args.seats,
    }
    print(
        f"licensee={args.sub!r} seats={args.seats} "
        f"expires {(now + timedelta(days=args.days)).date()}",
        file=sys.stderr,
    )
    print(jwt.encode(claims, key, algorithm="EdDSA"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)

    p_keygen = sub.add_parser("keygen", help="generate a signing keypair")
    p_keygen.add_argument("--out", type=Path, default=DEFAULT_KEY_PATH)
    p_keygen.set_defaults(func=keygen)

    p_issue = sub.add_parser("issue", help="sign a flat ee license")
    p_issue.add_argument("--sub", required=True, help="licensee name")
    p_issue.add_argument("--seats", type=int, required=True, help="max distinct principals")
    p_issue.add_argument("--days", type=int, default=365, help="validity period")
    p_issue.add_argument("--key", type=Path, default=DEFAULT_KEY_PATH)
    p_issue.set_defaults(func=issue)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
