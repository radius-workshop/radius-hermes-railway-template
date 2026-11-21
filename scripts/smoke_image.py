#!/usr/bin/env python3
"""Check the built HTTP server with an isolated, unfunded fixture identity."""
import json
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

# plugins/feedback-ledger captures Slack 👍/👎 clicks through these pinned Hermes details. Check them
# in a clean interpreter (outside /app, whose plugins/ directory would shadow Hermes' package).
import subprocess

FEEDBACK_CONTRACT = """
from hermes_cli.plugins import PluginContext
from plugins.platforms.slack.adapter import SlackAdapter
assert callable(getattr(PluginContext, "register_platform_handler", None)), "register_platform_handler missing"
element = SlackAdapter._feedback_block(None)["elements"][0]
assert element["type"] == "feedback_buttons" and element["action_id"] == "hermes_feedback", element
assert element["positive_button"]["value"] == "positive", element
assert element["negative_button"]["value"] == "negative", element
# plugins/slack-react reads the conversation and the triggering message from these session variables.
from gateway import session_context
names = {var.name for var in session_context._SESSION_VARS}
for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_THREAD_ID",
             "HERMES_SESSION_MESSAGE_ID", "HERMES_SESSION_SCOPE_ID"):
    assert name in names, name + " missing; update plugins/slack-react"
assert callable(session_context.get_session_env), "get_session_env missing"
"""
contract = subprocess.run([sys.executable, "-c", FEEDBACK_CONTRACT], cwd="/", capture_output=True, text=True)
assert contract.returncode == 0, "Hermes Slack feedback contract changed; update plugins/feedback-ledger:\n" + contract.stderr

# plugins/slack-home publishes the Slack App Home tab through these pinned Hermes details.
HOME_CONTRACT = """
from hermes_cli import plugins
from plugins.platforms.slack.adapter import SlackAdapter
for name in ("register_platform_handler", "register_slack_action_handler", "register_hook", "dispatch_tool"):
    assert callable(getattr(plugins.PluginContext, name, None)), name + " missing"
assert "post_tool_call" in plugins.VALID_HOOKS, "post_tool_call hook missing"
assert callable(getattr(SlackAdapter, "_is_interactive_user_authorized", None)), "Slack authorization check missing"
"""
home_contract = subprocess.run([sys.executable, "-c", HOME_CONTRACT], cwd="/", capture_output=True, text=True)
assert home_contract.returncode == 0, "Hermes Slack home contract changed; update plugins/slack-home:\n" + home_contract.stderr

# `sbc` is the wrapper around the pinned SBC CLI; it must start as the runtime user.
build_info = json.loads(Path("/app/build-info.json").read_text())
sbc_version = subprocess.run(["sbc", "--version"], capture_output=True, text=True)
assert sbc_version.returncode == 0, "sbc did not start:\n" + sbc_version.stderr
assert sbc_version.stdout.strip() == build_info["sbc_cli_version"], sbc_version.stdout

# The egress role's rendered config must load in the bundled iron-proxy.
import socket
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

print(f"Template {VERSION}: discovery, stable fixture identity, local auth, A2A denial, Slack feedback contract and egress proxy verified")
