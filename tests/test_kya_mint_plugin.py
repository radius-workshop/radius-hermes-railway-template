"""Tests for the kya-mint plugin (LLM-facing wrapper around kya_verify.mint_token)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
KYA_VERIFY_PATH = REPO_ROOT / "scripts" / "agent_server" / "kya_verify.py"
PLUGIN_PATH = REPO_ROOT / "plugins" / "kya-mint" / "__init__.py"


def _load(name, path):
    _spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(_spec)
    sys.modules[name] = mod
    _spec.loader.exec_module(mod)
    return mod


# Load kya_verify before the plugin so the plugin's `import kya_verify` resolves
# to our copy.
kya_verify = _load("kya_verify", KYA_VERIFY_PATH)
kya_mint_plugin = _load("kya_mint_plugin", PLUGIN_PATH)


class DummyCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}


class KyaMintPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kya-mint-plugin-")
        cls._prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = cls.tmp
        os.environ["PUBLIC_URL"] = "https://hermes.example"
        os.environ["AGENT_PUBLIC_IP"] = "8.8.8.8"
        os.environ["AGENT_NAME"] = "Test Hermes"
        kya_verify._KYA_PRIVATE_KEY = None
        kya_verify._KYA_KID = None
        kya_verify._KYA_JWKS = None
        kya_verify.reset_caches()

    @classmethod
    def tearDownClass(cls):
        if cls._prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = cls._prev_home

    def setUp(self):
        self.ctx = DummyCtx()
        kya_mint_plugin.register(self.ctx)
        self.handler = self.ctx.tools["generate_kya_token"]["handler"]
        kya_verify.reset_caches()

    def test_registers_tool(self):
        self.assertIn("generate_kya_token", self.ctx.tools)
        self.assertEqual(self.ctx.tools["generate_kya_token"]["toolset"], "kya-mint")

    def test_mint_with_minimal_params(self):
        out = json.loads(self.handler({"audience": "https://peer.example"}))
        self.assertNotIn("error", out, msg=out)
        self.assertEqual(out["header"]["typ"], "kya+jwt")
        self.assertEqual(out["header"]["alg"], "ES256")
        self.assertEqual(out["payload"]["iss"], "https://hermes.example")
        self.assertEqual(out["payload"]["aud"], "https://peer.example")
        self.assertEqual(out["payload"]["aid"]["name"], "Test Hermes")
        self.assertEqual(out["payload"]["aid"]["creation_ip"], "8.8.8.8")
        # Self-verification ran by default.
        self.assertTrue(out["self_verification"]["ok"])
        self.assertTrue(out["self_verification"]["signature_verified"])

    def test_mint_round_trips_against_real_verify(self):
        out = json.loads(self.handler({"audience": "https://peer.example"}))
        token = out["token"]
        # Now verify with the same kya_verify module + the in-process JWKS.
        report = kya_verify.verify_token(
            token_type="kya",
            token=token,
            expected_audience="https://peer.example",
            enforce_audience=True,
            enforce_replay_protection=False,
            jwks_override=kya_verify.get_kya_jwks(),
        )
        self.assertTrue(report.ok, msg=report.to_dict())

    def test_mint_requires_audience(self):
        out = json.loads(self.handler({}))
        self.assertIn("error", out)
        self.assertIn("audience", out["error"].lower())

    def test_mint_with_pay_object(self):
        out = json.loads(
            self.handler(
                {
                    "audience": "https://peer.example",
                    "token_type": "kya-pay",
                    "pay": {
                        "amt": "0.50",
                        "cur": "USD",
                        "stp": "coin",
                        "sti": {"chain": "radius"},
                    },
                }
            )
        )
        self.assertNotIn("error", out, msg=out)
        self.assertEqual(out["header"]["typ"], "kya-pay+jwt")
        self.assertEqual(out["payload"]["amt"], "0.50")
        self.assertEqual(out["payload"]["stp"], "coin")

    def test_mint_explicit_subject_and_aid(self):
        out = json.loads(
            self.handler(
                {
                    "audience": "https://peer.example",
                    "subject": "did:web:hermes.example",
                    "aid_name": "Custom",
                    "aid_creation_ip": "1.1.1.1",
                    "aid_source_ips": ["1.1.1.0/24", "agent.example"],
                    "environment": "sandbox",
                    "seller_domain": "peer.example",
                    "ttl_seconds": 60,
                }
            )
        )
        self.assertNotIn("error", out, msg=out)
        self.assertEqual(out["payload"]["sub"], "did:web:hermes.example")
        self.assertEqual(out["payload"]["aid"]["name"], "Custom")
        self.assertEqual(out["payload"]["aid"]["creation_ip"], "1.1.1.1")
        self.assertEqual(out["payload"]["aid"]["source_ips"], ["1.1.1.0/24", "agent.example"])
        self.assertEqual(out["payload"]["env"], "sandbox")
        self.assertEqual(out["payload"]["sdm"], "peer.example")
        self.assertEqual(
            out["payload"]["exp"] - out["payload"]["iat"], 60
        )

    def test_mint_rejects_private_creation_ip(self):
        out = json.loads(
            self.handler(
                {
                    "audience": "https://peer.example",
                    "aid_creation_ip": "10.0.0.1",
                }
            )
        )
        self.assertIn("error", out)
        self.assertIn("public", out["error"].lower())

    def test_mint_fails_without_issuer_default(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ["HERMES_HOME"] = self.tmp
            os.environ["AGENT_PUBLIC_IP"] = "8.8.8.8"
            os.environ["AGENT_NAME"] = "Test Hermes"
            out = json.loads(self.handler({"audience": "https://peer.example"}))
        self.assertIn("error", out)
        self.assertIn("issuer", out["error"].lower())


if __name__ == "__main__":
    unittest.main()
