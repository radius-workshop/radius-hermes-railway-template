#!/usr/bin/env python3
"""Check the built HTTP server with an isolated, unfunded fixture identity."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# This fixed public test key never accesses the deployed wallet or gateway.
os.environ["RADIUS_PRIVATE_KEY"] = "0x" + "01" * 32
os.environ["JWT_API_KEY"] = "isolated-container-smoke-key"
os.environ["TRUSTED_DIDS"] = ""
os.environ["AGENT_SERVER_MOCK_DATA"] = "1"

with tempfile.TemporaryDirectory(prefix="radius-hermes-smoke-") as home:
    os.environ["HERMES_HOME"] = home
    os.environ["A2A_SESSION_ROOT"] = str(Path(home) / "a2a-sessions")
    from fastapi.testclient import TestClient
    from scripts.agent_server.main import app
    from scripts.agent_server.version import VERSION

    did = None
    for _ in range(2):
        with TestClient(app) as client:
            response = client.get("/livez")
            assert response.status_code == 200, response.text
            assert response.json() == {"status": "ok", "version": VERSION}
            card = client.get("/.well-known/agent-card.json")
            assert card.status_code == 200, card.text
            assert card.json()["version"] == VERSION
            identity = client.get("/.well-known/did.json")
            assert identity.status_code == 200, identity.text
            current_did = identity.json()["id"]
            assert did is None or did == current_did, "DID changed across restart"
            did = current_did
            assert client.get("/health").status_code == 401
            assert client.post("/a2a", json={}).status_code == 401
            issued = client.post("/token", headers={"X-Api-Key": os.environ["JWT_API_KEY"]})
            assert issued.status_code == 200, "local token exchange failed"
            token = issued.json()["token"]
            health = client.get("/health", headers={"Authorization": "Bearer " + token})
            assert health.status_code == 200, "local token rejected"
            assert health.json()["version"] == VERSION

# The egress role's rendered config must load in the bundled iron-proxy.
import socket
import subprocess
import time

sys.path.insert(0, "/app/scripts/egress")
import iron_egress

with tempfile.TemporaryDirectory(prefix="radius-hermes-egress-") as state:
    cert, key = iron_egress.generate_ca()
    proxy_env = {
        "PATH": os.environ["PATH"], "HERMES_SERVICE_ROLE": "egress", "EGRESS_STATE_DIR": state,
        "EGRESS_TOKEN_SEED": "smoke-" * 8, "EGRESS_CA_CERT": cert, "EGRESS_CA_KEY": key,
        "EGRESS_SECRETS": "OPENROUTER_API_KEY,SLACK_BOT_TOKEN,SLACK_APP_TOKEN,TELEGRAM_BOT_TOKEN",
        "OPENROUTER_API_KEY": "fixture", "SLACK_BOT_TOKEN": "fixture", "SLACK_APP_TOKEN": "fixture",
        "TELEGRAM_BOT_TOKEN": "fixture", "EGRESS_LISTEN_HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(["/app/scripts/entrypoint.sh"], env=proxy_env)
    try:
        for _ in range(50):
            assert proc.poll() is None, "iron-proxy exited; rendered config rejected"
            try:
                socket.create_connection(("127.0.0.1", iron_egress.DEFAULT_PORT), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("iron-proxy did not listen on the tunnel port")
    finally:
        proc.terminate()
        proc.wait(timeout=10)

print(f"Template {VERSION}: discovery, stable fixture identity, local auth, A2A denial and egress proxy verified")
