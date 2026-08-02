"""Tests for the kya-spec plugin (verification mode)."""

from __future__ import annotations

import base64
import importlib.util
import json
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric.ec import (
    SECP256R1,
    EllipticCurvePrivateKey,
    generate_private_key,
)


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "plugins" / "kya-spec" / "__init__.py"
)
KYA_VERIFY_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "agent_server" / "kya_verify.py"
)

# Load kya_verify first so the plugin (which imports it) picks up our copy.
_kv_spec = importlib.util.spec_from_file_location("kya_verify", KYA_VERIFY_PATH)
kya_verify = importlib.util.module_from_spec(_kv_spec)
assert _kv_spec and _kv_spec.loader
import sys as _sys
_sys.modules["kya_verify"] = kya_verify
_kv_spec.loader.exec_module(kya_verify)

spec = importlib.util.spec_from_file_location("kya_spec_plugin", PLUGIN_PATH)
kya_spec_plugin = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(kya_spec_plugin)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_jwk(public_key, kid: str) -> dict:
    nums = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "alg": "ES256",
        "use": "sig",
        "x": _b64url(nums.x.to_bytes(32, "big")),
        "y": _b64url(nums.y.to_bytes(32, "big")),
    }


def _sign(payload: dict, headers: dict, key: EllipticCurvePrivateKey) -> str:
    return pyjwt.encode(payload, key, algorithm="ES256", headers=headers)


def _base_kya_payload(*, aud: str = "seller-abc", iss: str = "https://issuer.example") -> dict:
    now = int(time.time())
    return {
        "iss": iss,
        "sub": "buyer-123",
        "aud": aud,
        "iat": now - 10,
        "exp": now + 3600,
        "jti": str(uuid.uuid4()),
        "aid": {"name": "deal-finder", "creation_ip": "8.8.8.10"},
    }


class DummyCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class KyaSpecPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = generate_private_key(SECP256R1())
        cls.public_key = cls.private_key.public_key()
        cls.kid = "test-kid-1"
        cls.jwks = {"keys": [_make_jwk(cls.public_key, cls.kid)]}

    def setUp(self):
        self.ctx = DummyCtx()
        kya_spec_plugin.register(self.ctx)
        self.handler = self.ctx.tools["kya_validate_claims"]["handler"]
        # Reset module-level caches between tests so jti replay is per-test.
        kya_verify.reset_caches()

    def _build(
        self,
        *,
        payload_overrides: dict | None = None,
        header_overrides: dict | None = None,
    ) -> tuple[str, dict, dict]:
        payload = _base_kya_payload()
        if payload_overrides:
            payload.update(payload_overrides)
        headers = {"alg": "ES256", "kid": self.kid, "typ": "kya+jwt"}
        if header_overrides:
            headers.update(header_overrides)
        token = _sign(payload, headers, self.private_key)
        return token, headers, payload

    def _run(self, token: str, **kwargs) -> dict:
        params = {"token_type": "kya", "token": token, "expected_audience": "seller-abc"}
        params.update(kwargs)
        # Patch the JWKS HTTP fetch on kya_verify to return our fixture.
        with patch.object(kya_verify, "_http_get_json", return_value=self.jwks):
            return json.loads(self.handler(params))

    # ---- Registration ----

    def test_registers_only_validate_tool(self):
        self.assertIn("kya_validate_claims", self.ctx.tools)
        self.assertNotIn("kya_spec_lookup", self.ctx.tools)
        self.assertEqual(self.ctx.tools["kya_validate_claims"]["toolset"], "kya-spec")

    # ---- Happy path ----

    def test_valid_token_with_real_signature(self):
        token, _, _ = self._build()
        report = self._run(token)
        self.assertTrue(report["ok"], msg=report)
        self.assertTrue(report["signature_verified"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["issuer"], "https://issuer.example")
        self.assertEqual(report["kid"], self.kid)

    # ---- Signature verification ----

    def test_tampered_payload_fails_signature(self):
        token, _, _ = self._build()
        header_b64, payload_b64, sig = token.split(".")
        # Re-encode payload with mutated aud.
        decoded = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        decoded["aud"] = "attacker-seller"
        new_payload_b64 = base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
        tampered = f"{header_b64}.{new_payload_b64}.{sig}"
        report = self._run(tampered, expected_audience="attacker-seller")
        self.assertFalse(report["ok"])
        self.assertFalse(report["signature_verified"])
        self.assertTrue(any("Signature verification failed" in e for e in report["errors"]))

    def test_unknown_kid_fails(self):
        token, _, _ = self._build(header_overrides={"kid": "nope"})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertFalse(report["signature_verified"])
        self.assertTrue(any("kid" in e for e in report["errors"]))

    def test_unsupported_alg_rejected(self):
        # Build an HS256 token to ensure alg policy rejects it.
        headers = {"alg": "HS256", "kid": self.kid, "typ": "kya+jwt"}
        payload = _base_kya_payload()
        token = pyjwt.encode(payload, "secret", algorithm="HS256", headers=headers)
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("Unsupported JWT algorithm" in e for e in report["errors"]))

    # ---- Shape / spec compliance ----

    def test_typ_mismatch_with_token_type(self):
        token, _, _ = self._build(header_overrides={"typ": "pay+jwt"})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("typ='kya+jwt'" in e for e in report["errors"]))

    def test_jti_must_be_uuid(self):
        token, _, _ = self._build(payload_overrides={"jti": "not-a-uuid"})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("UUID" in e for e in report["errors"]))

    def test_iss_must_be_url(self):
        token, _, _ = self._build(payload_overrides={"iss": "not-a-url"})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("'iss' must be an http(s) URL" in e for e in report["errors"]))

    def test_expired_token(self):
        now = int(time.time())
        token, _, _ = self._build(payload_overrides={"iat": now - 7200, "exp": now - 3600})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("expired" in e for e in report["errors"]))

    def test_future_iat_rejected(self):
        now = int(time.time())
        token, _, _ = self._build(payload_overrides={"iat": now + 3600, "exp": now + 7200})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("future" in e for e in report["errors"]))

    def test_audience_must_match(self):
        token, _, _ = self._build()
        report = self._run(token, expected_audience="someone-else")
        self.assertFalse(report["ok"])
        self.assertTrue(any("'aud'" in e for e in report["errors"]))

    def test_audience_required_by_default(self):
        token, _, _ = self._build()
        # Don't pass expected_audience at all and don't override default.
        with patch.object(kya_verify, "_http_get_json", return_value=self.jwks):
            report = json.loads(
                self.handler({"token_type": "kya", "token": token})
            )
        self.assertFalse(report["ok"])
        self.assertTrue(any("MUST validate 'aud'" in e for e in report["errors"]))

    def test_env_mismatch_is_error(self):
        token, _, _ = self._build(payload_overrides={"env": "sandbox"})
        report = self._run(token, expected_environment="production")
        self.assertFalse(report["ok"])
        self.assertTrue(any("env" in e for e in report["errors"]))

    # ---- KYA sub-claims ----

    def test_aid_creation_ip_must_be_public(self):
        token, _, _ = self._build(
            payload_overrides={"aid": {"name": "x", "creation_ip": "10.0.0.1"}}
        )
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("public IPv4 or IPv6" in e for e in report["errors"]))

    def test_aid_missing_fields(self):
        token, _, _ = self._build(payload_overrides={"aid": {}})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("aid.name" in e for e in report["errors"]))
        self.assertTrue(any("aid.creation_ip" in e for e in report["errors"]))

    def test_hid_requires_email_when_present(self):
        token, _, _ = self._build(payload_overrides={"hid": {"given_name": "Alice"}})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("hid.email" in e for e in report["errors"]))

    def test_apd_requires_id_and_name_when_present(self):
        token, _, _ = self._build(payload_overrides={"apd": {"name": "Acme"}})
        report = self._run(token)
        self.assertFalse(report["ok"])
        self.assertTrue(any("apd.id" in e for e in report["errors"]))

    # ---- Trusted-issuer allowlist ----

    def test_trusted_issuer_allowlist_blocks(self):
        token, _, _ = self._build()
        report = self._run(token, trusted_issuers="https://other.example")
        self.assertFalse(report["ok"])
        self.assertTrue(any("trusted-issuer allowlist" in e for e in report["errors"]))

    def test_trusted_issuer_allowlist_allows(self):
        token, _, _ = self._build()
        report = self._run(token, trusted_issuers="https://issuer.example,https://other.example")
        self.assertTrue(report["ok"], msg=report)

    # ---- Replay protection ----

    def test_jti_replay_rejected_on_second_use(self):
        token, _, _ = self._build()
        first = self._run(token)
        self.assertTrue(first["ok"], msg=first)
        second = self._run(token)
        self.assertFalse(second["ok"])
        self.assertTrue(any("Replay detected" in e for e in second["errors"]))

    def test_replay_protection_can_be_disabled(self):
        token, _, _ = self._build()
        first = self._run(token)
        self.assertTrue(first["ok"])
        second = self._run(token, enforce_replay_protection=False)
        self.assertTrue(second["ok"], msg=second)


if __name__ == "__main__":
    unittest.main()
