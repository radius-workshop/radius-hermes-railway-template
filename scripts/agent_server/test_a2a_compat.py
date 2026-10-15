import unittest
from pathlib import Path

from scripts.agent_server import a2a_compat


class A2ACompatTests(unittest.TestCase):
    def test_standard_jsonrpc_error_payloads_use_stable_codes(self) -> None:
        cases = [
            (a2a_compat.JSONParseError(message="Parse error"), -32700, "Parse error"),
            (a2a_compat.InvalidRequestError(), -32600, None),
            (
                a2a_compat.MethodNotFoundError(message="No such method"),
                -32601,
                "No such method",
            ),
            (
                a2a_compat.InvalidParamsError(message="Bad params"),
                -32602,
                "Bad params",
            ),
            (
                a2a_compat.InternalError(message="Backend failed"),
                -32603,
                "Backend failed",
            ),
        ]

        for error_obj, expected_code, expected_message in cases:
            with self.subTest(error=type(error_obj).__name__):
                payload = a2a_compat.jsonrpc_error_payload(error_obj)

            self.assertEqual(payload["code"], expected_code)
            if expected_message is not None:
                self.assertEqual(payload["message"], expected_message)
            else:
                self.assertIsInstance(payload["message"], str)
                self.assertTrue(payload["message"])

    def test_a2a_root_error_wrapper_is_unwrapped_when_present(self) -> None:
        try:
            from a2a.types import A2AError
        except Exception:
            self.skipTest("a2a-sdk is not installed")

        wrapped = A2AError(root=a2a_compat.JSONParseError(message="Parse error"))

        self.assertEqual(
            a2a_compat.jsonrpc_error_payload(wrapped),
            {"code": -32700, "message": "Parse error"},
        )

    def test_agent_server_does_not_import_private_a2a_error_helpers(self) -> None:
        server_root = Path(__file__).resolve().parent
        production_sources = [
            path
            for path in server_root.glob("*.py")
            if not path.name.startswith("test_")
        ]

        for source_path in production_sources:
            with self.subTest(source=source_path.name):
                source = source_path.read_text(encoding="utf-8")

                self.assertNotIn("a2a.utils.errors", source)
                self.assertNotIn("JSON_RPC_ERROR_CODE_MAP", source)


if __name__ == "__main__":
    unittest.main()
