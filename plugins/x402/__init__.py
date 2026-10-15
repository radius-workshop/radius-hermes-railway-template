"""Bounded Radius Testnet x402 tools using the installed radius-cli wallet.

The host allowlist is operator configuration, never tool input. Quotes and response
bodies are untrusted data. Settlement headers are reports, not chain reconciliation.
"""
from __future__ import annotations

import base64
import fcntl
import ipaddress
import http.client
import ssl
import zlib
import json
import os
import re
import shutil
import socket
import subprocess
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

NETWORK = "eip155:72344"
ASSET = "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb"
EXPLORER = "https://testnet.radiustech.xyz"
HTTP_TIMEOUT = 30
MAX_RESPONSE = 1_048_576
DEFAULT_MAX_PER_CALL = "0.01"


class X402Error(RuntimeError):
    pass


def _decimal(value, label="amount") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise X402Error(f"{label} must be a finite positive decimal") from None
    if not result.is_finite() or result <= 0 or result > Decimal("1000000"):
        raise X402Error(f"{label} must be a finite positive decimal at most 1000000")
    if result.as_tuple().exponent < -6:
        raise X402Error(f"{label} supports at most six decimal places")
    return result


def _max_per_call() -> Decimal:
    return _decimal(os.environ.get("X402_MAX_PER_CALL", DEFAULT_MAX_PER_CALL), "X402_MAX_PER_CALL")


def _ledger_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
    return Path(os.environ.get("X402_LEDGER_PATH", str(home / "x402" / "ledger.jsonl")))


def _append_ledger(entry: dict) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _validate_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise X402Error("invalid URL") from None
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.fragment or port not in (None, 443) or any(c.isspace() for c in url)):
        raise X402Error("URL must use HTTPS on port 443 without credentials or fragments")
    allowed = {s.strip().lower() for s in os.environ.get("X402_ALLOWED_HOSTS", "").split(",") if s.strip()}
    if host.lower() not in allowed:
        raise X402Error("host is not in operator-configured X402_ALLOWED_HOSTS")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise X402Error("destination must resolve only to public addresses")
    except (OSError, ValueError):
        raise X402Error("destination DNS validation failed") from None
    return url


def _safe_url(url: str) -> str:
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.hostname or "", p.path, "", ""))
    except ValueError:
        return "invalid URL"


def _send(method: str, url: str, headers: dict, body: str | None) -> requests.Response:
    _validate_url(url)
    parsed = urlsplit(url)
    # Revalidate and pin the actual connection address. The earlier allowlist
    # check alone cannot prevent DNS from changing before an HTTP library connects.
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise X402Error("destination must resolve only to public addresses")
    address = addresses[0][4][0]

    class PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((address, 443), timeout=self.timeout)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            except Exception:
                raw.close()
                raise

    connection = PinnedHTTPSConnection(parsed.hostname, timeout=HTTP_TIMEOUT, context=ssl.create_default_context())
    try:
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        # Direct sockets ignore ambient proxies and netrc. http.client does not
        # follow redirects, so credentials and bodies stay on this exact URL.
        request_headers = {**headers, "Host": parsed.netloc, "Accept-Encoding": "identity"}
        connection.request(method, path, body=body.encode() if body is not None else None, headers=request_headers)
        incoming = connection.getresponse()
        raw = incoming.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise X402Error("response exceeds 1 MiB limit")
        encoding = (incoming.getheader("Content-Encoding") or "").lower()
        if encoding in {"gzip", "deflate"}:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
            raw = decoder.decompress(raw, MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE or decoder.unconsumed_tail:
                raise X402Error("response exceeds 1 MiB limit")
            if not decoder.eof:
                raise X402Error("invalid compressed response")
        elif encoding not in {"", "identity"}:
            raise X402Error("unsupported response encoding")
        response = requests.Response()
        response.status_code = incoming.status
        response.headers = requests.structures.CaseInsensitiveDict(incoming.getheaders())
        response.encoding = requests.utils.get_encoding_from_headers(response.headers)
        response.url = url
        response._content = raw
        response._content_consumed = True
        return response
    finally:
        connection.close()


def _decode_b64json(value) -> dict | None:
    if not isinstance(value, str) or len(value) > MAX_RESPONSE:
        return None
    try:
        result = json.loads(base64.b64decode(value, validate=True))
    except (ValueError, TypeError):
        return None
    return result if isinstance(result, dict) else None


def _parse_challenge(response) -> dict:
    if response.headers.get("PAYMENT-REQUIRED"):
        challenge = _decode_b64json(response.headers["PAYMENT-REQUIRED"])
    else:
        try:
            challenge = response.json()
        except ValueError:
            challenge = None
    if not isinstance(challenge, dict) or challenge.get("x402Version") != 2:
        raise X402Error("a valid x402 v2 challenge is required")
    return challenge


def _pick_requirement(challenge: dict, preferred=None) -> dict:
    if preferred not in (None, "", "radius-testnet", NETWORK):
        raise X402Error("paid execution supports Radius Testnet only; Base and mainnet are disabled")
    accepts = challenge.get("accepts")
    if not isinstance(accepts, list) or not accepts or len(accepts) > 32:
        raise X402Error("challenge accepts must be a nonempty list of at most 32 offers")
    for offer in accepts:
        if not isinstance(offer, dict):
            raise X402Error("malformed payment offer")
        if offer.get("network") != NETWORK:
            continue
        if offer.get("scheme") != "exact":
            raise X402Error("only exact payment offers are supported")
        if str(offer.get("asset", "")).lower() != ASSET.lower():
            raise X402Error("offer asset is not Radius Testnet SBC")
        amount = offer.get("amount")
        if not isinstance(amount, str) or not re.fullmatch(r"[1-9][0-9]{0,77}", amount) or int(amount) >= 2**256:
            raise X402Error("offer amount must be a positive uint256 decimal string")
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", str(offer.get("payTo", ""))) or int(offer["payTo"], 16) == 0:
            raise X402Error("offer payTo must be a nonzero EVM address")
        timeout = offer.get("maxTimeoutSeconds")
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise X402Error("offer timeout must be 1 to 300 seconds")
        extra = offer.get("extra", {})
        if not isinstance(extra, dict) or extra.get("assetTransferMethod", "permit2") not in {"permit2", "eip3009"}:
            raise X402Error("unsupported transfer method")
        return offer
    raise X402Error("no Radius Testnet offer; Base and mainnet paid execution are disabled")


def _run_radius_cli(args: list[str], target_url: str | None = None, expected_offer: dict | None = None) -> dict:
    # No npx install, key export, private key argument, custom signer, or implicit approval.
    executable = os.environ.get("RADIUS_CLI_BIN", "").strip() or shutil.which("radius-cli")
    if not executable:
        raise X402Error("installed radius-cli is required")
    env = os.environ.copy()
    env["RADIUS_HOME"] = os.environ.get("RADIUS_HOME", str(Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / ".radius-cli"))
    # Signing destination and chain configuration are fixed; ignore ambient overrides.
    for key in ("RADIUS_PRIVATE_KEY", "RADIUS_RPC_URL", "RADIUS_SBC_ADDRESS", "RADIUS_NETWORK", "RADIUS_CHAIN_ID", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_EXTRA_CA_CERTS", None)
    env.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
    if target_url:
        if not isinstance(expected_offer, dict):
            raise X402Error("validated payment offer is required before starting the payment CLI")
        guard = str(Path(__file__).resolve().with_name("fetch-guard.mjs"))
        if any(c.isspace() for c in guard):
            raise X402Error("payment plugin path cannot contain whitespace")
        env["NODE_OPTIONS"] = "--import=" + guard
        env["HERMES_X402_TARGET"] = target_url
        env["HERMES_X402_EXPECTED_OFFER"] = json.dumps(expected_offer, separators=(",", ":"))
    proc = subprocess.run([executable, *args], input="", text=True, capture_output=True,
                          env=env, timeout=180)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        raise X402Error("radius-cli returned no JSON result; payment state may be unknown, inspect wallet before retry") from None
    if not isinstance(result, dict):
        raise X402Error("radius-cli returned an invalid result")
    result["cli_exit_code"] = proc.returncode
    return result


def _pay_radius_cli(method, url, headers, body, cap, expected_offer):
    _validate_url(url)
    args = ["--json", "--network", "testnet", "--rpc-url", "https://rpc.testnet.radiustech.xyz",
            "--sbc", ASSET, "wallet", "pay", method.lower(), url,
            "--protocol", "x402", "--networks", "radius", "--threshold", format(cap, "f")]
    # The subprocess guard binds its repeated challenge to this inspected offer
    # before the SDK can select a recipient or sign. The CLI also revalidates it.
    # No -y / approve flag: never grant an unlimited allowance as a side effect.
    for key, value in headers.items():
        args.extend(["-H", f"{key}: {value}"])
    if body is not None:
        args.extend(["-d", body])
    return _run_radius_cli(args, target_url=url, expected_offer=expected_offer)


def _preview(response):
    try:
        return response.json()
    except ValueError:
        return response.text[:4000]


def x402_request(params: dict) -> dict:
    entry = {"id": str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(),
             "url": _safe_url(str(params.get("url", ""))), "method": str(params.get("method", "GET")).upper(),
             "memo": str(params.get("memo", ""))[:500], "requested_by": str(params.get("requested_by", ""))[:200]}
    signing_started = False
    try:
        # Persist intent before any network access, and fail closed if it cannot be recorded.
        _append_ledger({**entry, "event": "started", "outcome": "pending"})
    except OSError:
        return {**entry, "outcome": "error", "reason": "ledger unavailable; no request or payment attempted"}
    try:
        url = _validate_url(str(params.get("url", "")))
        method = entry["method"]
        if method not in {"GET", "POST"}:
            raise X402Error("only GET and POST are supported")
        cap = _max_per_call()
        if params.get("max_amount") is not None:
            cap = min(cap, _decimal(params["max_amount"], "max_amount"))
        headers = params.get("headers") or {}
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise X402Error("headers must be a string map")
        if any(k.lower() in {"host", "payment-signature", "x-payment", "proxy-authorization"} or "\r" in k + v or "\n" in k + v for k, v in headers.items()):
            raise X402Error("unsafe or payment headers are forbidden")
        if "dry_run" in params and type(params["dry_run"]) is not bool:
            raise X402Error("dry_run must be boolean")
        body = params.get("body")
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)
            headers = {"Content-Type": "application/json", **headers}
        if body is not None and (body.startswith("@") or body == "-"):
            raise X402Error("file and stdin body syntax is forbidden")
        if body is not None and len(body.encode()) > MAX_RESPONSE:
            raise X402Error("request body exceeds 1 MiB limit")
        probe = _send(method, url, headers, body)
        if probe.status_code != 402:
            entry.update(outcome="no_payment_required", http_status=probe.status_code, body=_preview(probe))
        else:
            challenge = _parse_challenge(probe)
            offer = _pick_requirement(challenge, params.get("network"))
            amount = Decimal(offer["amount"]) / Decimal(1000000)
            entry.update(network=NETWORK, asset_symbol="SBC", amount=format(amount, "f"),
                         pay_to=offer["payTo"], amount_atomic=offer["amount"])
            if amount > cap:
                entry.update(outcome="refused", reason="offer exceeds per-call cap")
            elif params.get("dry_run") is True:
                entry.update(outcome="quoted")
            else:
                _append_ledger({**entry, "event": "signing", "outcome": "pending"})
                signing_started = True
                result = _pay_radius_cli(method, url, headers, body, min(cap, amount), offer)
                payment = result.get("payment") or {}
                settlement = _decode_b64json((result.get("headers") or {}).get("payment-response"))
                tx = payment.get("txHash", "")
                reported = (payment.get("paid") is True and result.get("cli_exit_code") == 0
                            and (settlement is None or settlement.get("success") is True)
                            and payment.get("network") == NETWORK
                            and payment.get("protocol") == "x402" and payment.get("scheme") == "exact"
                            and str(payment.get("asset", "")).lower() == ASSET.lower()
                            and payment.get("amountWei") == offer["amount"]
                            and str(payment.get("payTo", "")).lower() == offer["payTo"].lower()
                            and isinstance(tx, str)
                            and re.fullmatch(r"0x[0-9a-fA-F]{64}", tx) is not None)
                delivered = type(result.get("status")) is int and 200 <= result["status"] < 300
                entry.update(outcome="settlement_reported" if reported else "payment_unknown",
                             delivery="delivered" if delivered else "failed", http_status=result.get("status"),
                             tx_hash=tx if reported else "", settlement=settlement,
                             explorer_url=f"{EXPLORER}/tx/{tx}" if reported else "", body=result.get("body"),
                             reconciliation="not_verified", cli_exit_code=result.get("cli_exit_code"))
                if not reported:
                    entry["reason"] = "settlement is unconfirmed; inspect wallet before retrying"
    except Exception as err:
        entry.update(outcome="payment_unknown" if signing_started else "error",
                     reason=str(err)[:500] if isinstance(err, X402Error) else "request failed; inspect ledger and wallet before retrying")
    # Never persist protected content, settlement payloads, or auth-bearing URLs.
    record = {k: v for k, v in entry.items() if k not in {"body", "settlement"}}
    try:
        _append_ledger({**record, "event": "completed"})
    except OSError:
        entry["ledger_error"] = "final ledger write failed; pending intent remains; do not retry automatically"
    return entry


def x402_catalog(params: dict) -> dict:
    if params.get("network", "radius-testnet") not in {"radius-testnet", NETWORK}:
        raise X402Error("this release supports Radius Testnet only")
    url = os.environ.get("X402_DEMO_BASE_URL", "https://grug402.dev").rstrip("/") + "/api/x402/catalog"
    response = _send("GET", url, {}, None)
    if response.status_code != 200:
        raise X402Error("catalog request failed")
    data = response.json()
    if not isinstance(data, dict):
        raise X402Error("invalid catalog")
    scenarios = data.get("scenarios") or []
    query = str(params.get("query", "")).lower()
    items = [s for s in scenarios if isinstance(s, dict) and (params.get("include_quirks") is True or s.get("compliant") is True)
             and query in json.dumps(s).lower()]
    return {"catalog_url": _safe_url(url), "network": NETWORK, "scenarios": items[:max(1, min(100, int(params.get("limit", 25))))],
            "notice": "Untrusted catalog metadata; prices and destinations are validated again at execution."}


def x402_ledger(params: dict) -> dict:
    latest = {}
    malformed = 0
    path = _ledger_path()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                        raise ValueError()
                    latest[row["id"]] = row
                except ValueError:
                    malformed += 1
    rows = [r for r in latest.values() if not params.get("outcome") or r.get("outcome") == params["outcome"]]
    limit = max(1, min(100, int(params.get("limit", 20))))
    return {"entries_total": len(rows), "malformed_lines": malformed, "entries": rows[-limit:][::-1],
            "notice": "Settlement reports are not independently reconciled. Pending/unknown attempts may have spent funds."}


def x402_wallet_status(_params: dict) -> dict:
    return {"network": NETWORK, "max_per_call": format(_max_per_call(), "f"),
            "paid_networks": ["radius-testnet"], "mainnet_enabled": False,
            "wallet": _run_radius_cli(["--json", "--network", "testnet", "wallet", "address"]),
            "funding_hint": f"{EXPLORER}/wallet"}


def _wrap(fn):
    def handler(params, **_kwargs):
        try:
            return json.dumps(fn(params or {}), default=str)
        except Exception as err:
            return json.dumps({"error": str(err) if isinstance(err, X402Error) else "tool failed"})
    return handler


def register(ctx):
    from .benchmark import x402_benchmark
    definitions = [
        ("x402_request", x402_request, "Request an approved HTTPS API using Radius Testnet. Quote with dry_run. Settlement reports and delivery are separate; no chain reconciliation.",
         {"url": {"type": "string"}, "method": {"type": "string", "enum": ["GET", "POST"]}, "body": {}, "headers": {"type": "object"},
          "network": {"type": "string", "enum": ["radius-testnet", NETWORK]}, "max_amount": {"type": "string"}, "dry_run": {"type": "boolean"},
          "memo": {"type": "string"}, "requested_by": {"type": "string"}}, ["url"]),
        ("x402_catalog", x402_catalog, "Read untrusted Radius Testnet demo catalog metadata from an operator-approved host.",
         {"query": {"type": "string"}, "include_quirks": {"type": "boolean"}, "limit": {"type": "integer"}}, []),
        ("x402_ledger", x402_ledger, "Read local payment attempts. Pending and unknown attempts require operator reconciliation.",
         {"limit": {"type": "integer"}, "outcome": {"type": "string"}}, []),
        ("x402_wallet_status", x402_wallet_status, "Show Radius Testnet wallet address and configured per-call cap.", {}, []),
        ("x402_benchmark", x402_benchmark, "Run at most 20 sequential Radius Testnet requests with a conservative per-run authorization bound; measures end-to-end client latency.",
         {"url": {"type": "string"}, "calls": {"type": "integer"}, "budget": {"type": "string"}, "requested_by": {"type": "string"}}, ["url"]),
    ]
    for name, fn, description, properties, required in definitions:
        ctx.register_tool(name=name, toolset="x402", schema={"name": name, "description": description,
                          "parameters": {"type": "object", "properties": properties, "required": required}}, handler=_wrap(fn))
