#!/usr/bin/env python3
"""
Generate a self-signed DID JWT for A2A authentication.
Usage: python gen_jwt.py

Reads the signing key from RADIUS_PRIVATE_KEY, or exports it from the
radius-cli keystore under RADIUS_HOME when the env var is not inherited.
Derives a did:web DID from PUBLIC_URL or RAILWAY_PUBLIC_DOMAIN.

Output (JSON on stdout):
  { did, token }
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auth import setup_auth, get_did, issue_token
from radius_cli import export_private_key
from url_utils import get_base_url


async def main() -> None:
    raw_key = os.environ.get("RADIUS_PRIVATE_KEY", "")
    if not raw_key:
        # The terminal environment may not inherit RADIUS_PRIVATE_KEY even though
        # the container has it; read the same key from the radius-cli keystore.
        try:
            raw_key = export_private_key()
        except Exception as err:
            print(f"RADIUS_PRIVATE_KEY is not set and radius-cli export failed: {err}", file=sys.stderr)
            sys.exit(1)
        # setup_auth() reads the key from the environment.
        os.environ["RADIUS_PRIVATE_KEY"] = raw_key

    await setup_auth(get_base_url())
    did = get_did()
    token = await issue_token("hermes")
    print(json.dumps({"did": did, "token": token}))


if __name__ == "__main__":
    asyncio.run(main())
