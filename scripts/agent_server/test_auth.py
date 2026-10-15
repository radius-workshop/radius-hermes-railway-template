"""Regression tests for the A2A authorization and token replay boundaries.

Run: python -m unittest discover -s scripts/agent_server -p test_auth.py
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import AsyncMock, Mock, patch

import jwt
from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth
import gen_jwt


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {
            "RADIUS_PRIVATE_KEY": "0x" + "01" * 32,
            "TRUSTED_DIDS": "",
        })
        self.environment.start()
        await auth.setup_auth("https://local.example")
        self.peer_did = "did:web:peer.example"
        self.peer_key = auth.private_key_from_hex("0x" + "02" * 32)

    async def asyncTearDown(self):
        self.environment.stop()

    def peer_token(self, **claims):
        payload = {
            "iss": self.peer_did, "sub": "peer",
            "exp": int(auth.time.time()) + 300,
        }
        payload.update(claims)
        return jwt.encode(payload, self.peer_key, algorithm="ES256K")

    async def authenticate(self, token):
        request = Request({"type": "http", "headers": [
            (b"authorization", f"Bearer {token}".encode()),
        ]})
        return await auth.jwt_auth_dep(request)

    async def test_default_and_empty_allowlists_reject_peer_without_resolution(self):
        for value in (None, "", "  ,  "):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("TRUSTED_DIDS", None)
                else:
                    os.environ["TRUSTED_DIDS"] = value
                with patch.object(auth, "resolve_did_to_public_key", new_callable=AsyncMock) as resolve:
                    with self.assertRaises(HTTPException) as error:
                        await self.authenticate(self.peer_token())
                    self.assertEqual(error.exception.status_code, 403)
                    resolve.assert_not_awaited()

    async def test_explicit_peer_can_authenticate_with_legacy_expiring_token(self):
        os.environ["TRUSTED_DIDS"] = self.peer_did
        with patch.object(auth, "resolve_did_to_public_key", new_callable=AsyncMock,
                          return_value=self.peer_key.public_key()):
            result = await self.authenticate(self.peer_token())
        self.assertEqual(result["issuer"], self.peer_did)

    async def test_allowlisted_issuer_still_requires_valid_signature(self):
        os.environ["TRUSTED_DIDS"] = self.peer_did
        with patch.object(auth, "resolve_did_to_public_key", new_callable=AsyncMock,
                          return_value=auth._private_key.public_key()):
            with self.assertRaises(HTTPException):
                await self.authenticate(self.peer_token())

    async def test_expiry_required_and_expired_tokens_rejected(self):
        os.environ["TRUSTED_DIDS"] = self.peer_did
        missing_exp = jwt.encode({"iss": self.peer_did}, self.peer_key, algorithm="ES256K")
        with patch.object(auth, "resolve_did_to_public_key", new_callable=AsyncMock,
                          return_value=self.peer_key.public_key()):
            for token in (missing_exp, self.peer_token(exp=int(auth.time.time()) - 1)):
                with self.subTest(token=token), self.assertRaises(HTTPException):
                    await self.authenticate(token)

    async def test_local_exchange_token_works_but_outbound_and_old_tokens_cannot_replay(self):
        local_token = await auth.issue_token("client")
        self.assertEqual((await self.authenticate(local_token))["issuer"], auth.get_did())
        for token in (
            await auth.issue_outbound_token(),
            await auth.issue_outbound_token(audience=auth.get_did()),
            auth._create_jwt("hermes", auth.get_did(), auth._private_key),
        ):
            with self.subTest(token=token), self.assertRaises(HTTPException):
                await self.authenticate(token)

    async def test_peer_audience_must_match_recipient_if_present(self):
        os.environ["TRUSTED_DIDS"] = self.peer_did
        with patch.object(auth, "resolve_did_to_public_key", new_callable=AsyncMock,
                          return_value=self.peer_key.public_key()):
            await self.authenticate(self.peer_token(aud=auth.get_did()))
            with self.assertRaises(HTTPException):
                await self.authenticate(self.peer_token(aud="did:web:other.example"))

    async def test_resolution_failure_returns_forbidden(self):
        os.environ["TRUSTED_DIDS"] = self.peer_did
        with patch.object(auth, "resolve_did_to_public_key", new_callable=AsyncMock,
                          side_effect=RuntimeError("unreachable")):
            with self.assertRaises(HTTPException) as error:
                await self.authenticate(self.peer_token())
        self.assertEqual(error.exception.status_code, 403)

    async def test_managed_worker_sends_outbound_token_that_cannot_replay_locally(self):
        # Import with isolated state, so no real wallet, sessions, or peers are used.
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {"HERMES_HOME": state}):
            from scripts.agent_server import main
        response = Mock()
        response.json.return_value = {"result": {"context_id": "context-1"}}
        client = AsyncMock()
        client.post.return_value = response
        with patch.object(main._a2a_session_store, "get_remote_api_key", return_value=""), \
                patch.object(main.httpx, "AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = client
            await main._send_managed_a2a_turn({
                "session_id": "session-1", "context_id": "context-1",
                "remote_agent": "https://peer.example",
            }, "test turn")
        token = client.post.call_args.kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, auth._private_key.public_key(), algorithms=["ES256K"])
        self.assertEqual(claims["token_use"], "a2a_outbound")
        with self.assertRaises(HTTPException):
            await self.authenticate(token)

    async def test_invalid_issuer_types_are_rejected(self):
        for issuer in (None, 123, [self.peer_did]):
            # Use JWS directly: recent PyJWT encoders reject malformed claims.
            payload = json.dumps({"iss": issuer, "exp": int(auth.time.time()) + 300}).encode()
            token = jwt.api_jws.encode(payload, self.peer_key, algorithm="ES256K")
            with self.subTest(issuer=issuer), self.assertRaises(HTTPException):
                await self.authenticate(token)

    async def test_cli_fallback_uses_exported_wallet_key_for_outbound_token(self):
        os.environ.pop("RADIUS_PRIVATE_KEY", None)
        os.environ["PUBLIC_URL"] = "https://local.example"
        output = io.StringIO()
        key_hex = "0x" + "03" * 32
        with patch.object(gen_jwt, "export_private_key", return_value=key_hex), \
                contextlib.redirect_stdout(output):
            await gen_jwt.main(audience=self.peer_did)
        response = json.loads(output.getvalue())
        claims = jwt.decode(response["token"], auth.private_key_from_hex(key_hex).public_key(),
                            algorithms=["ES256K"], audience=self.peer_did)
        self.assertEqual(claims["token_use"], "a2a_outbound")
        self.assertEqual(response["did"], "did:web:local.example")


if __name__ == "__main__":
    unittest.main()
