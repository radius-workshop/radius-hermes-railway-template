"""Browser hardening helpers for the public agent server surface."""
from __future__ import annotations

from urllib.parse import quote

from fastapi.responses import Response

BASE_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

HTML_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


def apply_browser_security_headers(response: Response, path: str) -> Response:
    for name, value in BASE_SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if path == "/":
        response.headers.setdefault("Content-Security-Policy", HTML_CSP)
    return response


def wallet_explorer_link(address: str | None) -> str:
    if not address:
        return "https://testnet.radiustech.xyz"
    return f"https://testnet.radiustech.xyz/address/{quote(address, safe='')}"
