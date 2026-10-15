"""Seller-side x402 v2 payment gate for POST /a2a (Radius Testnet, SBC).

Off unless A2A_X402_ENABLED is true. Hand-rolled because radius-sdk's seller
(`radius-sdk/server`, `radius-sdk/hono`, 0.6.0) is TypeScript for Node >= 22,
while this server is Python/FastAPI. The wire format mirrors radius-sdk and
@x402/core: an unpaid request gets 402 with a base64 JSON `PAYMENT-REQUIRED`
header (also returned as the JSON body); the paid retry carries
`PAYMENT-SIGNATURE`; the payment is checked by the facilitator's POST /verify,
the request runs, and a successful response is settled with POST /settle and
returned with a `PAYMENT-RESPONSE` header.

The facilitator checks the signature and submits the transfer. This module only
uses the wallet's public address (payTo) and never reads key material.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from logging_utils import log_event

logger = logging.getLogger(__name__)

X402_VERSION = 2
NETWORK = "eip155:72344"  # Radius Testnet
ASSET = "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb"  # SBC
ASSET_DECIMALS = 6
# EIP-3009 transferWithAuthorization: the buyer needs only SBC, no Permit2 approval.
EXTRA = {"assetTransferMethod": "eip3009", "name": "Stable Coin", "version": "1"}
MAX_TIMEOUT_SECONDS = 300
DEFAULT_PRICE_SBC = "0.001"
MAX_PRICE_SBC = Decimal("1")
DEFAULT_FACILITATOR_URL = "https://facilitator.testnet.radiustech.xyz"
PAYMENT_HEADERS = ("PAYMENT-SIGNATURE", "X-PAYMENT")
MAX_HEADER_BYTES = 16 * 1024
FACILITATOR_TIMEOUT = 30.0

# Tests may swap in an httpx.MockTransport.
_transport: Optional[httpx.AsyncBaseTransport] = None
# Payment payloads verified but not yet released, keyed by digest. Stops one
# signed authorization from buying several concurrent requests before settle.
# Per process only.
_in_flight: dict[str, float] = {}


class PaymentRequired(Exception):
    """Raised from the /a2a dependency; carries the response to send."""

    def __init__(self, response: Response):
        super().__init__(response.status_code)
        self.response = response


def enabled() -> bool:
    return os.environ.get("A2A_X402_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def price_sbc() -> Decimal:
    raw = os.environ.get("A2A_PRICE_SBC", "").strip() or DEFAULT_PRICE_SBC
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError("A2A_PRICE_SBC must be a decimal SBC amount") from None
    if not value.is_finite() or value <= 0 or value > MAX_PRICE_SBC:
        raise ValueError(f"A2A_PRICE_SBC must be greater than 0 and at most {MAX_PRICE_SBC}")
    if value.as_tuple().exponent < -ASSET_DECIMALS:
        raise ValueError(f"A2A_PRICE_SBC has more than {ASSET_DECIMALS} decimals")
    return value


def price_atomic() -> str:
    return str(int(price_sbc().scaleb(ASSET_DECIMALS)))


def facilitator_url() -> str:
    url = (os.environ.get("A2A_X402_FACILITATOR_URL", "").strip() or DEFAULT_FACILITATOR_URL).rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("A2A_X402_FACILITATOR_URL must be https")
    return url


def _valid_pay_to(address: Optional[str]) -> bool:
    return bool(address and re.fullmatch(r"0x[0-9a-fA-F]{40}", address) and int(address, 16) != 0)


def requirements(pay_to: str) -> dict[str, Any]:
    return {
        "scheme": "exact",
        "network": NETWORK,
        "amount": price_atomic(),
        "asset": ASSET,
        "payTo": pay_to,
        "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
        "extra": dict(EXTRA),
    }


def resource_info(url: str) -> dict[str, Any]:
    return {"url": url, "description": "A2A JSON-RPC message", "mimeType": "application/json"}


def offer_summary(pay_to: Optional[str], resource_url: str) -> Optional[dict[str, Any]]:
    """Public price advertisement for discovery documents; None when unavailable."""
    if not enabled() or not _valid_pay_to(pay_to):
        return None
    try:
        reqs = requirements(pay_to)
        facilitator = facilitator_url()
    except ValueError:
        return None
    return {
        "x402Version": X402_VERSION,
        "resource": resource_url,
        "price": f"{price_sbc().normalize():f} SBC",
        "facilitator": facilitator,
        "accepts": [reqs],
        "alternative_to": "bearer_jwt",
    }


def _b64json(value: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _payment_required(pay_to: str, resource_url: str, error: str) -> PaymentRequired:
    body = {
        "x402Version": X402_VERSION,
        "error": error,
        "resource": resource_info(resource_url),
        "accepts": [requirements(pay_to)],
    }
    return PaymentRequired(JSONResponse(
        body,
        status_code=402,
        headers={"PAYMENT-REQUIRED": _b64json(body), "Cache-Control": "no-store"},
    ))


def _unavailable(error: str, status_code: int, message: str) -> PaymentRequired:
    return PaymentRequired(JSONResponse({"error": error, "message": message}, status_code=status_code))


def _decode_payment(header: str) -> Optional[dict[str, Any]]:
    if len(header) > MAX_HEADER_BYTES:
        return None
    try:
        decoded = json.loads(base64.b64decode(header, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _matches(required: dict[str, Any], accepted: Any) -> bool:
    """Same rule as @x402/core: core fields equal, required extra is a subset."""
    if not isinstance(accepted, dict):
        return False
    core = {k: v for k, v in required.items() if k != "extra"}
    accepted_core = {k: v for k, v in accepted.items() if k != "extra"}
    if core != accepted_core:
        return False
    accepted_extra = accepted.get("extra") or {}
    return isinstance(accepted_extra, dict) and all(
        accepted_extra.get(k) == v for k, v in required["extra"].items()
    )


def _digest(payment: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payment.get("payload"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _claim(digest: str) -> bool:
    now = time.time()
    for key, expires in list(_in_flight.items()):
        if expires <= now:
            _in_flight.pop(key, None)
    if digest in _in_flight:
        return False
    _in_flight[digest] = now + MAX_TIMEOUT_SECONDS
    return True


def _release(digest: str) -> None:
    _in_flight.pop(digest, None)


async def _facilitator_post(path: str, body: dict[str, Any]) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT, transport=_transport) as client:
        resp = await client.post(f"{facilitator_url()}{path}", json=body)
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data


async def authorize(request: Request, pay_to: Optional[str], resource_url: str) -> dict[str, Any]:
    """Return a verified payment context for this request, or raise PaymentRequired."""
    if not _valid_pay_to(pay_to):
        raise _unavailable("paid_a2a_unavailable", 503, "Agent wallet address is not available")
    try:
        required = requirements(pay_to)
        facilitator_url()
    except ValueError as exc:
        log_event(logger, logging.ERROR, "Paid A2A misconfigured", event="a2a.x402", outcome="error", x402_error=str(exc))
        raise _unavailable("paid_a2a_unavailable", 503, "Paid A2A is misconfigured") from None

    header = next((request.headers.get(name) for name in PAYMENT_HEADERS if request.headers.get(name)), None)
    if not header:
        raise _payment_required(pay_to, resource_url, "Payment required")

    payment = _decode_payment(header.strip())
    if not payment or payment.get("x402Version") != X402_VERSION:
        raise _payment_required(pay_to, resource_url, "invalid_payment: expected an x402 v2 PAYMENT-SIGNATURE")
    if not _matches(required, payment.get("accepted")):
        raise _payment_required(pay_to, resource_url, "invalid_payment: payment does not match the offered requirements")

    digest = _digest(payment)
    if not _claim(digest):
        raise _payment_required(pay_to, resource_url, "invalid_payment: payment already in use")

    body = {"x402Version": X402_VERSION, "paymentPayload": payment, "paymentRequirements": required}
    try:
        status, data = await _facilitator_post("/verify", body)
    except httpx.HTTPError as exc:
        _release(digest)
        log_event(logger, logging.WARNING, "Paid A2A verify unreachable", event="a2a.x402", outcome="error", x402_error=type(exc).__name__)
        raise _unavailable("facilitator_error", 502, "Payment facilitator is unreachable; nothing was charged") from None
    if not isinstance(data, dict) or "isValid" not in data:
        _release(digest)
        log_event(logger, logging.WARNING, "Paid A2A verify failed", event="a2a.x402", outcome="error", facilitator_status=status)
        raise _unavailable("facilitator_error", 502, "Payment facilitator returned an unexpected response; nothing was charged")
    if data.get("isValid") is not True:
        _release(digest)
        reason = str(data.get("invalidReason") or "invalid_payment")[:200]
        log_event(logger, logging.INFO, "Paid A2A payment rejected", event="a2a.x402", outcome="rejected", x402_reason=reason)
        raise _payment_required(pay_to, resource_url, reason)

    log_event(logger, logging.INFO, "Paid A2A payment verified", event="a2a.x402", outcome="verified",
              x402_payer=data.get("payer"), x402_amount=required["amount"])
    return {
        "payment": payment,
        "requirements": required,
        "payer": data.get("payer"),
        "digest": digest,
        "pay_to": pay_to,
        "resource_url": resource_url,
    }


def _delivered(response: Response) -> bool:
    if isinstance(response, StreamingResponse):
        return response.status_code < 400
    if response.status_code >= 400:
        return False
    try:
        body = json.loads(bytes(response.body))
    except (AttributeError, ValueError):
        return False
    return isinstance(body, dict) and "error" not in body


async def finalize(ctx: dict[str, Any], response: Response) -> Response:
    """Settle after a successful A2A response; leave failed requests unpaid.

    message/send and delegated submissions settle after the handler has produced
    its result. message/stream settles once the stream is accepted, before the
    body is sent, so PAYMENT-RESPONSE can be a response header.
    """
    if not _delivered(response):
        _release(ctx["digest"])
        log_event(logger, logging.INFO, "Paid A2A request failed; payment not settled", event="a2a.x402",
                  outcome="not_settled", x402_payer=ctx.get("payer"), status_code=response.status_code)
        return response

    body = {"x402Version": X402_VERSION, "paymentPayload": ctx["payment"], "paymentRequirements": ctx["requirements"]}
    try:
        status, data = await _facilitator_post("/settle", body)
    except httpx.HTTPError as exc:
        data, status = None, None
        error_type = type(exc).__name__
    else:
        error_type = None
    if not isinstance(data, dict) or "success" not in data:
        log_event(logger, logging.ERROR, "Paid A2A settle outcome unknown", event="a2a.x402", outcome="unknown",
                  x402_payer=ctx.get("payer"), facilitator_status=status, x402_error=error_type)
        return JSONResponse(
            {"error": "facilitator_error",
             "message": "Settlement outcome unknown; check the payer's transactions before retrying"},
            status_code=502,
        )
    if data.get("success") is not True:
        reason = str(data.get("errorReason") or "settlement_failed")[:200]
        log_event(logger, logging.WARNING, "Paid A2A settle rejected", event="a2a.x402", outcome="settle_failed",
                  x402_payer=ctx.get("payer"), x402_reason=reason)
        return _payment_required(ctx["pay_to"], ctx["resource_url"], reason).response

    log_event(logger, logging.INFO, "Paid A2A payment settled", event="a2a.x402", outcome="settled",
              x402_payer=data.get("payer") or ctx.get("payer"), x402_amount=ctx["requirements"]["amount"],
              x402_transaction=data.get("transaction"))
    response.headers["PAYMENT-RESPONSE"] = _b64json(data)
    response.headers["Access-Control-Expose-Headers"] = "PAYMENT-RESPONSE"
    return response
