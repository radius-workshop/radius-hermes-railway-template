"""Tests for /a2a auth mode selection (DID vs KYA)."""

from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException


class A2AAuthModesTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev_mock = os.environ.get("AGENT_SERVER_MOCK_DATA")
        os.environ["AGENT_SERVER_MOCK_DATA"] = "1"
        import scripts.agent_server.main as main_module

        cls.main = importlib.reload(main_module)

    @classmethod
    def tearDownClass(cls):
        if cls._prev_mock is None:
            os.environ.pop("AGENT_SERVER_MOCK_DATA", None)
        else:
            os.environ["AGENT_SERVER_MOCK_DATA"] = cls._prev_mock

    async def test_did_or_kya_allows_kya_without_did(self):
        request = AsyncMock()
        request.headers = {"skyfire-pay-id": "tok"}

        with patch.dict(os.environ, {"A2A_AUTH_MODE": "did_or_kya"}, clear=False):
            with patch.object(self.main, "jwt_auth_dep", AsyncMock(side_effect=HTTPException(status_code=401))):
                with patch.object(
                    self.main.kya_verify,
                    "evaluate_inbound",
                    return_value={
                        "action": "accept",
                        "report": None,
                        "reason": None,
                    },
                ):
                    auth = await self.main.a2a_auth_dep(request)

        self.assertTrue(auth["kya_ok"])
        self.assertFalse(auth["did_ok"])

    async def test_kya_only_rejects_when_no_kya(self):
        request = AsyncMock()
        request.headers = {}

        with patch.dict(os.environ, {"A2A_AUTH_MODE": "kya_only"}, clear=False):
            with patch.object(self.main, "jwt_auth_dep", AsyncMock(return_value={"issuer": "did:web:test"})):
                with patch.object(
                    self.main.kya_verify,
                    "evaluate_inbound",
                    return_value={
                        "action": "reject",
                        "report": None,
                        "reason": "kya_token_required_but_missing",
                    },
                ):
                    with self.assertRaises(HTTPException):
                        await self.main.a2a_auth_dep(request)

    async def test_did_and_kya_requires_both(self):
        request = AsyncMock()
        request.headers = {}

        with patch.dict(os.environ, {"A2A_AUTH_MODE": "did_and_kya"}, clear=False):
            with patch.object(self.main, "jwt_auth_dep", AsyncMock(return_value={"issuer": "did:web:test"})):
                with patch.object(
                    self.main.kya_verify,
                    "evaluate_inbound",
                    return_value={
                        "action": "reject",
                        "report": None,
                        "reason": "kya_verification_failed",
                    },
                ):
                    with self.assertRaises(HTTPException):
                        await self.main.a2a_auth_dep(request)


if __name__ == "__main__":
    unittest.main()
