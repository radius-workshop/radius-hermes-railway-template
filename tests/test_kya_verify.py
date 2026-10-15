"""Tests for the shared kya_verify module (mint + inbound policy)."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
KYA_VERIFY_PATH = REPO_ROOT / "scripts" / "agent_server" / "kya_verify.py"


def _load_kya_verify():
    _spec = importlib.util.spec_from_file_location("kya_verify", KYA_VERIFY_PATH)
    mod = importlib.util.module_from_spec(_spec)
    sys.modules["kya_verify"] = mod
    _spec.loader.exec_module(mod)
    return mod


kya_verify = _load_kya_verify()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class KyaMintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Isolate KYA keypair to a temp dir so we don't write to the real volume.
        cls.tmp = tempfile.mkdtemp(prefix="kya-mint-")
        cls._prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = cls.tmp
        # Reset module-level singletons so the keypair is regenerated under our tmp dir.
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
        kya_verify.reset_caches()

    # ---- Keypair / JWKS ----

    def test_setup_kya_key_is_idempotent(self):
        kid1, jwks1 = kya_verify.setup_kya_key()
        kid2, jwks2 = kya_verify.setup_kya_key()
        self.assertEqual(kid1, kid2)
        self.assertEqual(jwks1, jwks2)
        self.assertEqual(jwks1["keys"][0]["kty"], "EC")
        self.assertEqual(jwks1["keys"][0]["crv"], "P-256")
        self.assertEqual(jwks1["keys"][0]["alg"], "ES256")
        self.assertEqual(jwks1["keys"][0]["kid"], kid1)

    def test_key_file_persisted_with_safe_permissions(self):
        kya_verify.setup_kya_key()
        key_path = Path(self.tmp) / ".radius" / "kya" / "key.pem"
        self.assertTrue(key_path.exists())
        mode = key_path.stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, msg=f"key.pem has mode {oct(mode)}")

    # ---- mint_token ----

    def test_mint_kya_round_trip_verifies(self):
        out = kya_verify.mint_token(
            token_type="kya",
            issuer="https://hermes.example",
            subject="did:web:hermes.example",
            audience="https://peer.example",
            aid={"name": "Hermes Agent", "creation_ip": "8.8.8.8"},
            env="production",
        )
        report = kya_verify.verify_token(
            token_type="kya",
            token=out["token"],
            expected_audience="https://peer.example",
            expected_environment="production",
            trusted_issuers=None,
            enforce_audience=True,
            enforce_replay_protection=False,
            jwks_override=kya_verify.get_kya_jwks(),
        )
        self.assertTrue(report.ok, msg=report.to_dict())
        self.assertTrue(report.signature_verified)
        self.assertEqual(report.issuer, "https://hermes.example")
        self.assertEqual(report.errors, [])

    def test_mint_rejects_invalid_aid_creation_ip(self):
        with self.assertRaises(ValueError) as cm:
            kya_verify.mint_token(
                token_type="kya",
                issuer="https://hermes.example",
                subject="did:web:hermes.example",
                audience="https://peer.example",
                aid={"name": "x", "creation_ip": "10.0.0.1"},
            )
        self.assertIn("public", str(cm.exception).lower())

    def test_mint_rejects_missing_aid_for_kya(self):
        with self.assertRaises(ValueError):
            kya_verify.mint_token(
                token_type="kya",
                issuer="https://hermes.example",
                subject="did:web:hermes.example",
                audience="https://peer.example",
                aid=None,
            )

    def test_mint_rejects_reserved_extra_claim(self):
        with self.assertRaises(ValueError) as cm:
            kya_verify.mint_token(
                token_type="kya",
                issuer="https://hermes.example",
                subject="did:web:hermes.example",
                audience="https://peer.example",
                aid={"name": "x", "creation_ip": "8.8.8.8"},
                extra_claims={"aud": "attacker"},
            )
        self.assertIn("reserved", str(cm.exception).lower())

    def test_mint_kya_pay_requires_pay_object(self):
        with self.assertRaises(ValueError):
            kya_verify.mint_token(
                token_type="kya-pay",
                issuer="https://hermes.example",
                subject="did:web:hermes.example",
                audience="https://peer.example",
                aid={"name": "x", "creation_ip": "8.8.8.8"},
                pay=None,
            )

    def test_mint_kya_pay_round_trip(self):
        out = kya_verify.mint_token(
            token_type="kya-pay",
            issuer="https://hermes.example",
            subject="did:web:hermes.example",
            audience="https://peer.example",
            aid={"name": "Hermes", "creation_ip": "8.8.8.8"},
            pay={"amt": "1.50", "cur": "USD", "stp": "coin", "sti": {"chain": "radius"}},
        )
        self.assertEqual(out["header"]["typ"], "kya-pay+jwt")
        self.assertEqual(out["payload"]["amt"], "1.50")
        report = kya_verify.verify_token(
            token_type="kya-pay",
            token=out["token"],
            expected_audience="https://peer.example",
            enforce_audience=True,
            enforce_replay_protection=False,
            jwks_override=kya_verify.get_kya_jwks(),
        )
        self.assertTrue(report.ok, msg=report.to_dict())

    # ---- evaluate_inbound ----

    def test_evaluate_inbound_policy_off_skips(self):
        with patch.dict(os.environ, {"KYA_INBOUND_POLICY": "off"}, clear=False):
            decision = kya_verify.evaluate_inbound(token=None)
        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "policy_off")

    def test_evaluate_inbound_required_rejects_missing_token(self):
        decision = kya_verify.evaluate_inbound(token=None, policy="required")
        self.assertEqual(decision["action"], "reject")
        self.assertEqual(decision["reason"], "kya_token_required_but_missing")

    def test_evaluate_inbound_opportunistic_skips_missing_token(self):
        decision = kya_verify.evaluate_inbound(token=None, policy="opportunistic")
        self.assertEqual(decision["action"], "skip")

    def test_evaluate_inbound_accepts_minted_token(self):
        out = kya_verify.mint_token(
            token_type="kya",
            issuer="https://hermes.example",
            subject="did:web:hermes.example",
            audience="https://peer.example",
            aid={"name": "Hermes", "creation_ip": "8.8.8.8"},
        )
        with patch.object(kya_verify, "_http_get_json", return_value=kya_verify.get_kya_jwks()):
            decision = kya_verify.evaluate_inbound(
                token=out["token"],
                policy="required",
                trusted_issuers={"https://hermes.example"},
                expected_audience="https://peer.example",
            )
        self.assertEqual(decision["action"], "accept", msg=decision)
        self.assertTrue(decision["report"].ok)

    def test_evaluate_inbound_required_rejects_tampered_token(self):
        out = kya_verify.mint_token(
            token_type="kya",
            issuer="https://hermes.example",
            subject="did:web:hermes.example",
            audience="https://peer.example",
            aid={"name": "Hermes", "creation_ip": "8.8.8.8"},
        )
        header_b64, payload_b64, sig = out["token"].split(".")
        decoded = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
        decoded["aud"] = "https://attacker.example"
        new_payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(decoded).encode())
            .rstrip(b"=")
            .decode()
        )
        tampered = f"{header_b64}.{new_payload_b64}.{sig}"
        with patch.object(kya_verify, "_http_get_json", return_value=kya_verify.get_kya_jwks()):
            decision = kya_verify.evaluate_inbound(
                token=tampered,
                policy="required",
                trusted_issuers={"https://hermes.example"},
                expected_audience="https://attacker.example",
            )
        self.assertEqual(decision["action"], "reject")

    def test_evaluate_inbound_opportunistic_warns_on_failure(self):
        with patch.object(kya_verify, "_http_get_json", return_value={"keys": []}):
            decision = kya_verify.evaluate_inbound(
                token="not.a.realtoken",
                policy="opportunistic",
                trusted_issuers={"https://hermes.example"},
                expected_audience="https://peer.example",
            )
        self.assertEqual(decision["action"], "warn")

    def test_inbound_header_name_default(self):
        # Default per KYA spec.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KYA_INBOUND_HEADER", None)
            self.assertEqual(kya_verify.inbound_header_name(), "skyfire-pay-id")

    def test_inbound_header_name_override(self):
        with patch.dict(os.environ, {"KYA_INBOUND_HEADER": "x-my-kya"}, clear=False):
            self.assertEqual(kya_verify.inbound_header_name(), "x-my-kya")

    def test_parse_trusted_issuers_from_env(self):
        with patch.dict(
            os.environ,
            {"TRUSTED_KYA_ISSUERS": "https://a.example, https://b.example"},
            clear=False,
        ):
            result = kya_verify.parse_trusted_issuers(None)
        self.assertEqual(result, {"https://a.example", "https://b.example"})

    def test_parse_trusted_issuers_explicit_list(self):
        result = kya_verify.parse_trusted_issuers(["https://a.example", "https://b.example"])
        self.assertEqual(result, {"https://a.example", "https://b.example"})

    def test_wildcard_issuer_match(self):
        report = kya_verify._validate_shape(
            token_type="kya",
            header={"alg": "ES256", "kid": "k", "typ": "kya+jwt"},
            payload={
                "iss": "https://agent0.72344.xyz",
                "sub": "did:web:agent0.72344.xyz",
                "aud": "https://peer.example",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "jti": "11111111-1111-1111-1111-111111111111",
                "aid": {"name": "Agent", "creation_ip": "8.8.8.8"},
            },
            expected_audience="https://peer.example",
            expected_environment=None,
            trusted_issuers={"https://*.72344.xyz"},
            clock_skew=60,
            enforce_audience=True,
        )
        self.assertFalse(any("trusted-issuer allowlist" in e for e in report.errors), msg=report.errors)

    def test_wildcard_does_not_match_apex(self):
        report = kya_verify._validate_shape(
            token_type="kya",
            header={"alg": "ES256", "kid": "k", "typ": "kya+jwt"},
            payload={
                "iss": "https://72344.xyz",
                "sub": "did:web:72344.xyz",
                "aud": "https://peer.example",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "jti": "22222222-2222-2222-2222-222222222222",
                "aid": {"name": "Agent", "creation_ip": "8.8.8.8"},
            },
            expected_audience="https://peer.example",
            expected_environment=None,
            trusted_issuers={"https://*.72344.xyz"},
            clock_skew=60,
            enforce_audience=True,
        )
        self.assertTrue(any("trusted-issuer allowlist" in e for e in report.errors), msg=report.errors)

    def _mint_for_peer(self, token_type="kya"):
        return kya_verify.mint_token(
            token_type=token_type, issuer="https://hermes.example", subject="did:web:hermes.example",
            audience="https://peer.example", aid={"name": "Hermes", "creation_ip": "8.8.8.8"},
            pay={"amt": "1.50", "cur": "USD"} if token_type == "kya-pay" else None,
        )

    def test_missing_or_untrusted_issuer_never_fetches_jwks(self):
        token = self._mint_for_peer()["token"]
        for trusted in (None, set(), {"https://untrusted.example"}):
            with self.subTest(trusted=trusted), patch.object(kya_verify, "_http_get_json") as fetch:
                report = kya_verify.verify_token(token_type="kya", token=token,
                    expected_audience="https://peer.example", trusted_issuers=trusted)
                self.assertFalse(report.ok)
                fetch.assert_not_called()

    def test_inbound_requires_operator_allowlist(self):
        with patch.dict(os.environ, {"TRUSTED_KYA_ISSUERS": ""}), patch.object(kya_verify, "_http_get_json") as fetch:
            result = kya_verify.evaluate_inbound(token=self._mint_for_peer()["token"], policy="required")
        self.assertEqual(result["action"], "reject")
        self.assertEqual(result["reason"], "trusted_kya_issuers_required")
        fetch.assert_not_called()

    def test_combined_token_accepted_once(self):
        token = self._mint_for_peer("kya-pay")["token"]
        with patch.object(kya_verify, "_http_get_json", return_value=kya_verify.get_kya_jwks()):
            first = kya_verify.evaluate_inbound(token=token, policy="required", expected_audience="https://peer.example",
                trusted_issuers={"https://hermes.example"})
            second = kya_verify.evaluate_inbound(token=token, policy="required", expected_audience="https://peer.example",
                trusted_issuers={"https://hermes.example"})
        self.assertEqual(first["action"], "accept", first)
        self.assertEqual(second["action"], "reject", second)
        self.assertTrue(any("Replay" in e for e in second["report"].errors))

    def test_replay_cache_covers_expiry_clock_skew(self):
        import jwt
        out = self._mint_for_peer()
        out["payload"]["exp"] = int(time.time()) - 1
        token = jwt.encode(out["payload"], kya_verify._KYA_PRIVATE_KEY, algorithm="ES256", headers=out["header"])
        params = dict(token_type="kya", token=token, expected_audience="https://peer.example",
            jwks_override=kya_verify.get_kya_jwks(), clock_skew=60)
        self.assertTrue(kya_verify.verify_token(**params).ok)
        self.assertFalse(kya_verify.verify_token(**params).ok)

    def test_nonfinite_timestamps_are_invalid(self):
        for field in ("iat", "exp"):
            for value in (float("inf"), float("nan")):
                errors = []
                payload = {"iat": int(time.time()), "exp": int(time.time()) + 60, field: value}
                kya_verify._validate_timestamps(payload, errors, 60)
                self.assertTrue(errors)

    def test_issuer_allowlist_preserves_origin_and_path(self):
        patterns = {"https://*.example/Issuer"}
        self.assertTrue(kya_verify._issuer_allowed("https://peer.example/Issuer", patterns))
        for issuer in ("https://peer.example/issuer", "https://peer.example:8443/Issuer",
                "https://peer.example/Issuer?x=1", "https://user@peer.example/Issuer", "https://example/Issuer"):
            self.assertFalse(kya_verify._issuer_allowed(issuer, patterns), issuer)

    def test_private_or_mixed_dns_is_rejected(self):
        import socket
        with patch.object(kya_verify.socket, "getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                kya_verify._validate_outbound_jwks_url("https://issuer.example/.well-known/jwks.json")


if __name__ == "__main__":
    unittest.main()
