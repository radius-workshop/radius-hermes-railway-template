import unittest

from fastapi.responses import Response

from scripts.agent_server.security_headers import (
    apply_browser_security_headers,
    wallet_explorer_link,
)


class XSSHygieneTests(unittest.TestCase):
    def test_homepage_headers_include_csp_and_nosniff(self) -> None:
        response = apply_browser_security_headers(Response(), "/")

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("object-src 'none'", response.headers["Content-Security-Policy"])

    def test_non_html_routes_do_not_receive_homepage_csp(self) -> None:
        response = apply_browser_security_headers(Response(), "/.well-known/agent-card.json")

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Content-Security-Policy", response.headers)

    def test_wallet_link_encodes_untrusted_address_bytes(self) -> None:
        address = "0xabc'><script>alert(1)</script>"

        self.assertEqual(
            wallet_explorer_link(address),
            "https://testnet.radiustech.xyz/address/0xabc%27%3E%3Cscript%3Ealert%281%29%3C%2Fscript%3E",
        )


if __name__ == "__main__":
    unittest.main()
