"""x402 client tools for Hermes.

Routes each payment by the network the server's 402 challenge advertises:

- Radius (eip155:72344 / eip155:723487): delegated to `radius-cli wallet x402`, which
  signs Permit2 payments with EIP-2612 gas sponsoring.
- Base Sepolia / Base (eip155:84532 / eip155:8453): signed here as an EIP-3009
  `TransferWithAuthorization` with the same wallet key, since radius-cli only pays SBC on Radius.

Every attempt (paid, refused, or failed) is appended to a JSONL ledger on the persistent
volume so the agent can produce a paper trail later.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import requests

DEFAULT_DEMO_BASE_URL = "https://grug402.dev"
DEFAULT_MAX_PER_CALL = "0.01"
HTTP_TIMEOUT = 60

# network id -> settings. `rail` picks the signer.
NETWORKS: dict[str, dict] = {
    "eip155:72344": {
        "key": "radius-testnet",
        "label": "Radius Testnet",
        "rail": "radius-cli",
        "cli_network": "testnet",
        "rpc_url": "https://rpc.testnet.radiustech.xyz",
        "explorer": "https://testnet.radiustech.xyz",
        "asset_symbol": "SBC",
        "asset": "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb",
        "decimals": 6,
        "testnet": True,
        "catalog_prefix": "/api/x402",
    },
    "eip155:723487": {
        "key": "radius-mainnet",
        "label": "Radius Mainnet",
        "rail": "radius-cli",
        "cli_network": "mainnet",
        "rpc_url": "https://rpc.radiustech.xyz",
        "explorer": "https://network.radiustech.xyz",
        "asset_symbol": "USDC",
        "asset": "0x7bAB65D9D76df37117F56128F72103A78Bfa4B52",
        "decimals": 6,
        "testnet": False,
        "catalog_prefix": "/api/x402-mainnet",
    },
    "eip155:84532": {
        "key": "base-sepolia",
        "label": "Base Sepolia",
        "rail": "eip3009",
        "rpc_url": "https://sepolia.base.org",
        "explorer": "https://sepolia.basescan.org",
        "asset_symbol": "USDC",
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "decimals": 6,
        "testnet": True,
        "catalog_prefix": "/api/x402-base-sepolia",
    },
    "eip155:8453": {
        "key": "base",
        "label": "Base",
        "rail": "eip3009",
        "rpc_url": "https://mainnet.base.org",
        "explorer": "https://basescan.org",
        "asset_symbol": "USDC",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "decimals": 6,
        "testnet": False,
        "catalog_prefix": None,
    },
}
NETWORK_BY_KEY = {cfg["key"]: net for net, cfg in NETWORKS.items()}


class X402Error(RuntimeError):
    pass


# --------------------------------------------------------------------------- config


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _demo_base_url() -> str:
    return os.environ.get("X402_DEMO_BASE_URL", DEFAULT_DEMO_BASE_URL).rstrip("/")


def _max_per_call() -> Decimal:
    raw = os.environ.get("X402_MAX_PER_CALL", DEFAULT_MAX_PER_CALL)
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal(DEFAULT_MAX_PER_CALL)


def _allow_mainnet() -> bool:
    return _truthy(os.environ.get("X402_ALLOW_MAINNET"))


def _ledger_path() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    return Path(os.environ.get("X402_LEDGER_PATH", str(Path(hermes_home) / "x402" / "ledger.jsonl")))


def _radius_home() -> str:
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    return os.environ.get("RADIUS_HOME", str(Path(hermes_home) / ".radius-cli"))


def _radius_cli_cmd() -> list[str]:
    configured = os.environ.get("RADIUS_CLI_BIN", "").strip()
    if configured:
        return shlex.split(configured)
    resolved = shutil.which("radius-cli")
    if resolved:
        return [resolved]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "radius-cli"]
    raise X402Error("radius-cli is not installed or not on PATH.")


def _run_radius_cli(args: list[str], stdin: str = "") -> str:
    env = os.environ.copy()
    env["RADIUS_HOME"] = _radius_home()
    # Never let an interactive prompt hang a tool call: feed stdin explicitly (non-TTY).
    proc = subprocess.run(
        [*_radius_cli_cmd(), *args], input=stdin, capture_output=True, text=True, env=env, timeout=180
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise X402Error(msg or f"radius-cli exited with code {proc.returncode}")
    return (proc.stdout or "").strip()


def _private_key() -> str:
    key = os.environ.get("RADIUS_PRIVATE_KEY", "").strip()
    if not key:
        # Same approach as entrypoint.sh: confirm the export prompt and pull the key out.
        out = _run_radius_cli(["wallet", "export"], stdin="y\n")
        found = re.findall(r"0x[a-fA-F0-9]{64}", out)
        key = found[-1] if found else ""
    if not key:
        raise X402Error("No wallet key available (RADIUS_PRIVATE_KEY unset and radius-cli export failed).")
    return key if key.startswith("0x") else f"0x{key}"


def _wallet_address() -> str:
    from eth_account import Account

    return Account.from_key(_private_key()).address


# --------------------------------------------------------------------------- helpers


def _atomic_to_display(amount: str | int, decimals: int) -> str:
    value = Decimal(int(amount)) / (Decimal(10) ** decimals)
    return format(value.normalize(), "f")


def _b64json(value: dict) -> str:
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _decode_b64json(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        return json.loads(base64.b64decode(value))
    except Exception:
        return None


def _parse_challenge(resp: requests.Response) -> dict | None:
    challenge = _decode_b64json(resp.headers.get("PAYMENT-REQUIRED"))
    if challenge:
        return challenge
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("accepts"), list):
        return body
    return None


def _body_preview(resp: requests.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        text = resp.text or ""
        return text[:4000]


def _tx_from_settlement(settlement: dict | None) -> str:
    if not isinstance(settlement, dict):
        return ""
    return str(
        settlement.get("transaction")
        or settlement.get("txHash")
        or settlement.get("transactionHash")
        or ""
    )


def _append_ledger(entry: dict) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _send(method: str, url: str, headers: dict, body: str | None) -> requests.Response:
    return requests.request(
        method, url, headers=headers, data=body.encode() if body is not None else None,
        timeout=HTTP_TIMEOUT, allow_redirects=False,
    )


# --------------------------------------------------------------------------- payment rails


def _pay_eip3009(
    *, method: str, url: str, headers: dict, body: str | None, challenge: dict, req: dict, net: dict
) -> dict:
    from eth_account import Account

    extra = req.get("extra") or {}
    if (extra.get("assetTransferMethod") or "eip3009") != "eip3009":
        raise X402Error(
            f"{net['label']} offer uses assetTransferMethod={extra.get('assetTransferMethod')!r}; "
            "only eip3009 is supported on this rail."
        )
    if not extra.get("name") or not extra.get("version"):
        raise X402Error("Challenge is missing extra.name/extra.version (the token's EIP-712 domain).")

    account = Account.from_key(_private_key())
    now = int(time.time())
    authorization = {
        "from": account.address,
        "to": req["payTo"],
        "value": str(req["amount"]),
        "validAfter": "0",
        "validBefore": str(now + int(req.get("maxTimeoutSeconds") or 300)),
        "nonce": "0x" + secrets.token_hex(32),
    }
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": extra["name"],
            "version": extra["version"],
            "chainId": int(req["network"].split(":")[1]),
            "verifyingContract": req["asset"],
        },
        "message": {
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": bytes.fromhex(authorization["nonce"][2:]),
        },
    }
    signed = Account.sign_typed_data(account.key, full_message=typed)
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    version = int(challenge.get("x402Version") or 2)
    payload: dict = {
        "x402Version": version,
        "payload": {"signature": signature, "authorization": authorization},
    }
    if version >= 2:
        payload["accepted"] = req
        if challenge.get("resource"):
            payload["resource"] = challenge["resource"]
        if challenge.get("extensions"):
            payload["extensions"] = challenge["extensions"]
        header_name = "PAYMENT-SIGNATURE"
    else:
        payload["scheme"] = req.get("scheme", "exact")
        payload["network"] = req["network"]
        header_name = "X-PAYMENT"

    paid = _send(method, url, {**headers, header_name: _b64json(payload)}, body)
    settlement = _decode_b64json(
        paid.headers.get("PAYMENT-RESPONSE") or paid.headers.get("X-PAYMENT-RESPONSE")
    )
    return {
        "status": paid.status_code,
        "body": _body_preview(paid),
        "settlement": settlement,
        "payer": account.address,
    }


def _pay_radius_cli(
    *, method: str, url: str, headers: dict, body: str | None, net: dict, max_amount: Decimal
) -> dict:
    args = ["--json", "--network", net["cli_network"], "--rpc-url",
            os.environ.get("RADIUS_RPC_URL", net["rpc_url"]) if net["testnet"] else net["rpc_url"]]
    if net["key"] == "radius-mainnet":
        # radius-cli assumes SBC semantics behind --sbc; point it at the advertised mainnet asset.
        args += ["--sbc", net["asset"]]
    # --x402-approve-permit2: when the facilitator still reports a short Permit2 allowance
    # (PERMIT2_ALLOWANCE_REQUIRED), submit the one-time approval instead of failing. Each payment
    # remains individually capped by its signed Permit2 message and by --x402-threshold.
    args += ["wallet", "x402", method.lower(), url, "--x402-threshold", format(max_amount, "f"),
             "--x402-approve-permit2"]
    for k, v in headers.items():
        args += ["-H", f"{k}: {v}"]
    if body is not None:
        args += ["-d", body]
    out = _run_radius_cli(args)
    envelope = json.loads(out)
    hdrs = {str(k).lower(): v for k, v in (envelope.get("headers") or {}).items()}
    settlement = _decode_b64json(hdrs.get("payment-response")) or envelope.get("payment")
    raw_body = envelope.get("body")
    if isinstance(raw_body, str) and envelope.get("bodyEncoding") in (None, "utf8", "utf-8", "text"):
        try:
            raw_body = json.loads(raw_body)
        except ValueError:
            pass
    return {
        "status": envelope.get("status"),
        "body": raw_body,
        "settlement": settlement,
        "payment": envelope.get("payment"),
    }


# --------------------------------------------------------------------------- tool impls


def _pick_requirement(challenge: dict, preferred: str | None) -> tuple[dict, dict]:
    accepts = challenge.get("accepts") or []
    if not accepts:
        raise X402Error("402 challenge has an empty accepts list; nothing can be paid.")
    supported = [(r, NETWORKS[r.get("network")]) for r in accepts if r.get("network") in NETWORKS]
    if not supported:
        nets = sorted({str(r.get("network")) for r in accepts})
        raise X402Error(f"No supported network in challenge. Offered: {nets}. Supported: {sorted(NETWORKS)}.")
    if preferred:
        want = NETWORK_BY_KEY.get(preferred, preferred)
        for r, net in supported:
            if r.get("network") == want:
                return r, net
        raise X402Error(f"Server did not offer network {preferred!r}.")
    supported.sort(key=lambda pair: (not pair[1]["testnet"], pair[1]["rail"] != "radius-cli"))
    return supported[0]


def x402_request(params: dict) -> dict:
    url = str(params.get("url") or "").strip()
    if not url:
        raise X402Error("missing required parameter 'url'")
    if urlparse(url).scheme not in ("http", "https"):
        raise X402Error("url must be http(s)")
    method = str(params.get("method") or "GET").upper()
    body = params.get("body")
    if body is not None and not isinstance(body, str):
        body = json.dumps(body)
    headers = {str(k): str(v) for k, v in (params.get("headers") or {}).items()}
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
    cap = _max_per_call()
    if params.get("max_amount") not in (None, ""):
        cap = min(cap, Decimal(str(params["max_amount"])))
    memo = str(params.get("memo") or "").strip()
    dry_run = _truthy(str(params.get("dry_run", "")))

    entry: dict = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": method,
        "url": url,
        "memo": memo,
        "requested_by": str(params.get("requested_by") or ""),
    }

    try:
        probe = _send(method, url, headers, body)
        if probe.status_code != 402:
            entry.update(outcome="no_payment_required", http_status=probe.status_code)
            _append_ledger(entry)
            return {**entry, "body": _body_preview(probe)}

        challenge = _parse_challenge(probe)
        if not challenge:
            raise X402Error("Server returned 402 but no parseable x402 challenge (header or body).")
        req, net = _pick_requirement(challenge, params.get("network"))
        amount = _atomic_to_display(req["amount"], net["decimals"])
        entry.update(
            network=req["network"], network_label=net["label"], scheme=req.get("scheme"),
            asset=req["asset"], asset_symbol=net["asset_symbol"], amount=amount,
            amount_atomic=str(req["amount"]), pay_to=req["payTo"],
            transfer_method=(req.get("extra") or {}).get("assetTransferMethod"),
        )

        if not net["testnet"] and not _allow_mainnet():
            entry.update(outcome="refused", reason="mainnet payments disabled (set X402_ALLOW_MAINNET=true)")
        elif Decimal(amount) > cap:
            entry.update(outcome="refused", reason=f"price {amount} {net['asset_symbol']} exceeds cap {cap}")
        elif dry_run:
            entry.update(outcome="quoted")
        if entry.get("outcome"):
            _append_ledger(entry)
            return {**entry, "challenge": challenge}

        if net["rail"] == "radius-cli":
            result = _pay_radius_cli(method=method, url=url, headers=headers, body=body, net=net, max_amount=cap)
        else:
            result = _pay_eip3009(
                method=method, url=url, headers=headers, body=body,
                challenge=challenge, req=req, net=net,
            )

        tx = _tx_from_settlement(result.get("settlement"))
        ok = result.get("status") == 200
        entry.update(
            outcome="paid" if ok else "payment_failed",
            http_status=result.get("status"),
            tx_hash=tx,
            explorer_url=f"{net['explorer']}/tx/{tx}" if tx else "",
            payer=result.get("payer") or (result.get("settlement") or {}).get("payer", ""),
        )
        if not ok:
            entry["reason"] = json.dumps(result.get("body"))[:500]
        _append_ledger(entry)
        return {**entry, "body": result.get("body"), "settlement": result.get("settlement")}
    except Exception as err:  # noqa: BLE001 - every attempt must reach the ledger
        entry.update(outcome="error", reason=str(err)[:1000])
        _append_ledger(entry)
        return entry


def x402_catalog(params: dict) -> dict:
    network = str(params.get("network") or "radius-testnet").strip()
    net_id = NETWORK_BY_KEY.get(network, network)
    net = NETWORKS.get(net_id)
    if not net or not net.get("catalog_prefix"):
        raise X402Error(f"No demo catalog for network {network!r}. Use radius-testnet, base-sepolia, or radius-mainnet.")
    url = f"{_demo_base_url()}{net['catalog_prefix']}/catalog"
    data = requests.get(url, timeout=HTTP_TIMEOUT).json()
    include_quirks = _truthy(str(params.get("include_quirks", "")))
    query = str(params.get("query") or "").strip().lower()
    items = []
    for s in data.get("scenarios") or []:
        if not include_quirks and not s.get("compliant"):
            continue
        if query and query not in json.dumps([s.get("id"), s.get("title"), s.get("summary")]).lower():
            continue
        items.append({
            "id": s.get("id"), "method": s.get("method"), "url": s.get("url"),
            "title": s.get("title"), "price": _atomic_to_display(s.get("priceAtomic") or 0, net["decimals"]),
            "asset": net["asset_symbol"], "compliant": s.get("compliant"),
            "summary": s.get("summary"), "sample_body": s.get("sampleBody"),
        })
    limit = int(params.get("limit") or 25)
    return {
        "catalog_url": url, "network": net_id, "network_label": net["label"],
        "asset": data.get("asset"), "facilitator": data.get("facilitatorUrl"),
        "total_matching": len(items), "scenarios": items[:limit],
    }


def x402_ledger(params: dict) -> dict:
    path = _ledger_path()
    limit = int(params.get("limit") or 20)
    outcome = str(params.get("outcome") or "").strip()
    rows: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if outcome and row.get("outcome") != outcome:
                continue
            rows.append(row)
    totals: dict[str, Decimal] = {}
    for row in rows:
        if row.get("outcome") == "paid" and row.get("amount"):
            key = f"{row.get('asset_symbol')} on {row.get('network_label')}"
            totals[key] = totals.get(key, Decimal(0)) + Decimal(row["amount"])
    return {
        "ledger_path": str(path),
        "entries_total": len(rows),
        "paid_totals": {k: format(v.normalize(), "f") for k, v in totals.items()},
        "entries": rows[-limit:][::-1],
    }


def _erc20_balance(rpc_url: str, token: str, owner: str) -> int:
    data = "0x70a08231" + owner.lower().replace("0x", "").rjust(64, "0")
    res = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                       "params": [{"to": token, "data": data}, "latest"]}, timeout=20).json()
    return int(res.get("result") or "0x0", 16)


def _native_balance(rpc_url: str, owner: str) -> int:
    res = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                                       "params": [owner, "latest"]}, timeout=20).json()
    return int(res.get("result") or "0x0", 16)


def x402_wallet_status(_params: dict) -> dict:
    address = _wallet_address()
    networks = []
    for net_id, net in NETWORKS.items():
        if not net["testnet"] and not _allow_mainnet():
            continue
        row = {"network": net_id, "label": net["label"], "asset": net["asset_symbol"],
               "explorer": f"{net['explorer']}/address/{address}"}
        try:
            row["asset_balance"] = _atomic_to_display(_erc20_balance(net["rpc_url"], net["asset"], address), net["decimals"])
            if net["rail"] == "eip3009":
                row["native_eth"] = _atomic_to_display(_native_balance(net["rpc_url"], address), 18)
        except Exception as err:  # noqa: BLE001
            row["error"] = str(err)[:300]
        networks.append(row)
    return {
        "address": address,
        "max_per_call": format(_max_per_call(), "f"),
        "mainnet_enabled": _allow_mainnet(),
        "networks": networks,
        "funding_hints": {
            "radius-testnet": "Radius faucet: https://testnet.radiustech.xyz/wallet (or the dripping-faucet skill)",
            "base-sepolia": "Circle faucet: https://faucet.circle.com (select Base Sepolia, USDC). No ETH needed: EIP-3009 is gasless for the payer.",
        },
    }


# --------------------------------------------------------------------------- registration


def _wrap(fn):
    def handler(params, **_kwargs):
        try:
            return json.dumps(fn(params or {}), default=str)
        except Exception as err:  # noqa: BLE001
            return json.dumps({"error": str(err)})

    return handler


def register(ctx):
    ctx.register_tool(
        name="x402_request",
        toolset="x402",
        schema={
            "name": "x402_request",
            "description": (
                "Call an HTTP endpoint and, if it answers 402 Payment Required with an x402 challenge, pay it "
                "from this agent's wallet and return the paid response plus the on-chain settlement tx. "
                "Supports Radius (SBC via Permit2) and Base Sepolia (USDC via EIP-3009). Enforces the "
                "X402_MAX_PER_CALL cap, refuses mainnet unless enabled, and records every attempt in the ledger. "
                "Use dry_run=true to quote the price without paying."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full endpoint URL."},
                    "method": {"type": "string", "description": "HTTP method, default GET."},
                    "body": {"description": "Optional request body (JSON object or string) for POST/PUT."},
                    "headers": {"type": "object", "description": "Optional extra request headers."},
                    "network": {
                        "type": "string",
                        "description": "Optional network to pay on when several are offered: radius-testnet, base-sepolia, radius-mainnet, base.",
                    },
                    "max_amount": {
                        "type": "string",
                        "description": "Optional per-call cap in display units (e.g. '0.001'); can only lower the configured cap.",
                    },
                    "memo": {"type": "string", "description": "Why this payment is being made; stored in the ledger."},
                    "requested_by": {"type": "string", "description": "Who asked for it (e.g. Slack user); stored in the ledger."},
                    "dry_run": {"type": "boolean", "description": "Quote only; do not pay."},
                },
                "required": ["url"],
            },
        },
        handler=_wrap(x402_request),
    )
    ctx.register_tool(
        name="x402_catalog",
        toolset="x402",
        schema={
            "name": "x402_catalog",
            "description": (
                "List demo x402 endpoints from the grug402.dev reference marketplace for a network. "
                "Compliant (happy-path) endpoints only unless include_quirks=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "network": {"type": "string", "description": "radius-testnet (default), base-sepolia, or radius-mainnet."},
                    "query": {"type": "string", "description": "Optional text filter, e.g. 'weather' or 'haiku'."},
                    "include_quirks": {"type": "boolean", "description": "Include intentionally non-compliant endpoints."},
                    "limit": {"type": "integer", "description": "Max scenarios to return (default 25)."},
                },
                "required": [],
            },
        },
        handler=_wrap(x402_catalog),
    )
    ctx.register_tool(
        name="x402_ledger",
        toolset="x402",
        schema={
            "name": "x402_ledger",
            "description": "Read the persistent x402 payment ledger (newest first) with per-asset paid totals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max entries to return (default 20)."},
                    "outcome": {"type": "string", "description": "Filter: paid, refused, quoted, payment_failed, error, no_payment_required."},
                },
                "required": [],
            },
        },
        handler=_wrap(x402_ledger),
    )
    ctx.register_tool(
        name="x402_wallet_status",
        toolset="x402",
        schema={
            "name": "x402_wallet_status",
            "description": "Show this agent's wallet address and x402 payment-asset balances on every enabled network, plus funding hints.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=_wrap(x402_wallet_status),
    )
