#!/usr/bin/env python3
"""Check the built server without starting wallets, gateways, or paid calls."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from scripts.agent_server.main import app
from scripts.agent_server.version import VERSION


with TestClient(app) as client:
    response = client.get("/livez")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "version": VERSION}
    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200, card.text
    assert card.json()["version"] == VERSION
    assert client.get("/.well-known/did.json").status_code == 200
    assert client.get("/health").status_code == 401
print(f"Template {VERSION}: liveness, discovery, DID, and health authentication verified")
