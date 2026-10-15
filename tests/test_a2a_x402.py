"""Tests for the optional x402 payment gate on POST /a2a (facilitator mocked)."""

from __future__ import annotations

import base64
import importlib
import json
import os
import secrets
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

WALLET = "0x4D8020F43A9EFb829DBe4Cb93cbb29d5B52aEc6b"
PAYER = "0x1111111111111111111111111111111111111111"
SEND = {
    "jsonrpc": "2.0", "id": "1", "method": "message/send",
    "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": "hi"}]}},
}


def _b64(value) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def _unb64(value: str):
    return json.loads(base64.b64decode(value))


class FakeBridge:
    def __init__(self, events):
        self.events = events

    async def handle_send(self, rpc_id, message):
        self.events.append("handler")
        return {"result": {"id": "task-1", "context_id": "ctx-1", "status": {"state": "TASK_STATE_COMPLETED"}}}


class A2AX402Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["AGENT_SERVER_MOCK_DATA"] = "1"
        import scripts.agent_server.main as main_module

        cls.main = importlib.reload(main_module)
        cls.seller = cls.main.x402_seller

    def setUp(self):
        self.events = []
        self.facilitator_calls = []
        self.verify_result = {"isValid": True, "payer": PAYER}
        self.settle_result = {"success": True, "transaction": "0x" + "ab" * 32, "network": "eip155:72344", "payer": PAYER}
        self.env = patch.dict(os.environ, {
            "A2A_X402_ENABLED": "true",
            "A2A_PRICE_SBC": "0.001",
            "RADIUS_WALLET_ADDRESS": WALLET,
            "HERMES_API_KEY": "test-key",
            "A2A_AUTH_MODE": "did_only",
        })
        self.env.start()
        self.seller._in_flight.clear()
        self.seller._transport = httpx.MockTransport(self._facilitator)
        patches = [
            patch.object(self.main, "jwt_auth_dep", AsyncMock(side_effect=HTTPException(status_code=401))),
            patch.object(self.main, "_a2a_bridge", FakeBridge(self.events)),
            patch.object(self.main, "A2A_MODE", "direct"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(self.main.app)

    def tearDown(self):
        self.seller._transport = None
        self.env.stop()

    def _facilitator(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.facilitator_calls.append((request.url.path, body))
        self.events.append(request.url.path)
        assert str(request.url).startswith("https://facilitator.testnet.radiustech.xyz/")
        result = self.verify_result if request.url.path == "/verify" else self.settle_result
        return httpx.Response(200, json=result)

    def _challenge(self):
        response = self.client.post("/a2a", json=SEND)
        self.assertEqual(response.status_code, 402, response.text)
        return _unb64(response.headers["PAYMENT-REQUIRED"])

    def _payment(self, accepted):
        return _b64({
            "x402Version": 2,
            "resource": {"url": "https://agent.example.com/a2a"},
            "accepted": accepted,
            "payload": {
                "signature": "0x" + "11" * 65,
                "authorization": {
                    "from": PAYER, "to": WALLET, "value": accepted["amount"],
                    "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + secrets.token_hex(32),
                },
            },
        })

    def test_disabled_keeps_401(self):
        with patch.dict(os.environ, {"A2A_X402_ENABLED": ""}):
            response = self.client.post("/a2a", json=SEND)
            card = self.client.get("/.well-known/agent-card.json").json()
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("PAYMENT-REQUIRED", response.headers)
        self.assertNotIn("x402", card)
        self.assertEqual(self.facilitator_calls, [])

    def test_unpaid_request_gets_402_challenge(self):
        challenge = self._challenge()
        self.assertEqual(challenge["x402Version"], 2)
        self.assertEqual(challenge["resource"]["url"], "https://agent.example.com/a2a")
        offer = challenge["accepts"][0]
        self.assertEqual(offer, {
            "scheme": "exact", "network": "eip155:72344", "amount": "1000",
            "asset": "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb", "payTo": WALLET,
            "maxTimeoutSeconds": 300,
            "extra": {"assetTransferMethod": "eip3009", "name": "Stable Coin", "version": "1"},
        })
        self.assertEqual(self.events, [])

    def test_trusted_jwt_is_not_charged(self):
        with patch.object(self.main, "jwt_auth_dep", AsyncMock(return_value={"issuer": "did:web:peer.example"})):
            response = self.client.post("/a2a", json=SEND)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("PAYMENT-RESPONSE", response.headers)
        self.assertEqual(self.events, ["handler"])

    def test_paid_request_verifies_runs_then_settles(self):
        offer = self._challenge()["accepts"][0]
        header = self._payment(offer)
        response = self.client.post("/a2a", json=SEND, headers={"PAYMENT-SIGNATURE": header})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["id"], "task-1")
        self.assertEqual(self.events, ["/verify", "handler", "/settle"])
        receipt = _unb64(response.headers["PAYMENT-RESPONSE"])
        self.assertTrue(receipt["success"])
        verify_body = self.facilitator_calls[0][1]
        self.assertEqual(verify_body["paymentRequirements"], offer)
        self.assertEqual(verify_body["x402Version"], 2)
        # The same signed authorization cannot buy a second request.
        replay = self.client.post("/a2a", json=SEND, headers={"PAYMENT-SIGNATURE": header})
        self.assertEqual(replay.status_code, 402)
        self.assertEqual(len(self.facilitator_calls), 2)
        self.assertIn("already in use", replay.json()["error"])

    def test_underpaying_offer_is_rejected_before_facilitator(self):
        offer = {**self._challenge()["accepts"][0], "amount": "1"}
        response = self.client.post("/a2a", json=SEND, headers={"PAYMENT-SIGNATURE": self._payment(offer)})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(self.facilitator_calls, [])
        self.assertEqual(self.events, [])

    def test_invalid_payment_does_not_run_handler(self):
        self.verify_result = {"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER}
        offer = self._challenge()["accepts"][0]
        response = self.client.post("/a2a", json=SEND, headers={"PAYMENT-SIGNATURE": self._payment(offer)})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(_unb64(response.headers["PAYMENT-REQUIRED"])["error"], "insufficient_funds")
        self.assertEqual(self.events, ["/verify"])

    def test_failed_request_is_not_settled(self):
        offer = self._challenge()["accepts"][0]
        bad = {**SEND, "method": "tasks/get"}
        response = self.client.post("/a2a", json=bad, headers={"PAYMENT-SIGNATURE": self._payment(offer)})
        self.assertEqual(response.status_code, 200)
        self.assertIn("error", response.json())
        self.assertEqual(self.events, ["/verify"])
        self.assertEqual(self.seller._in_flight, {})

    def test_settle_failure_withholds_result(self):
        self.settle_result = {"success": False, "errorReason": "invalid_nonce", "transaction": "", "network": "eip155:72344"}
        offer = self._challenge()["accepts"][0]
        response = self.client.post("/a2a", json=SEND, headers={"PAYMENT-SIGNATURE": self._payment(offer)})
        self.assertEqual(response.status_code, 402)
        self.assertNotIn("result", response.json())
        self.assertEqual(self.events, ["/verify", "handler", "/settle"])

    def test_facilitator_unreachable_is_502(self):
        def down(request):
            raise httpx.ConnectError("down", request=request)

        self.seller._transport = httpx.MockTransport(down)
        offer = self._challenge()["accepts"][0]
        response = self.client.post("/a2a", json=SEND, headers={"PAYMENT-SIGNATURE": self._payment(offer)})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.events, [])

    def test_missing_wallet_is_503(self):
        with patch.dict(os.environ, {"RADIUS_WALLET_ADDRESS": ""}), \
                patch.object(self.main, "_radius_address_file", return_value=self.main.Path("/nonexistent/address")):
            response = self.client.post("/a2a", json=SEND)
        self.assertEqual(response.status_code, 503)

    def test_agent_card_advertises_price(self):
        with patch.dict(os.environ, {"A2A_PRICE_SBC": "0.0025"}):
            card = self.client.get("/.well-known/agent-card.json").json()
        self.assertEqual(card["x402"]["price"], "0.0025 SBC")
        self.assertEqual(card["x402"]["accepts"][0]["amount"], "2500")
        self.assertEqual(card["x402"]["accepts"][0]["payTo"], WALLET)

    def test_price_validation(self):
        for bad in ("0", "-1", "abc", "0.0000001", "5", "NaN"):
            with patch.dict(os.environ, {"A2A_PRICE_SBC": bad}), self.assertRaises(ValueError):
                self.seller.price_atomic()


if __name__ == "__main__":
    unittest.main()
