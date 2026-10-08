"""x402_benchmark: timed, concurrent x402 payments across networks.

Both networks go through the same in-process client (the official `x402` SDK), so the
comparison isolates the network + facilitator instead of client overhead. Each call is
split into phases:

- challenge_ms: unpaid request -> 402 challenge
- sign_ms:      building the signed payment payload (includes the SDK's RPC reads)
- paid_ms:      paid request -> response (server verify + facilitator settle + resource)
- total_ms:     all three
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

from . import (
    NETWORK_BY_KEY,
    NETWORKS,
    X402Error,
    _allow_mainnet,
    _append_ledger,
    _erc20_balance,
    _atomic_to_display,
    _demo_base_url,
    _ledger_path,
    _max_per_call,
    _parse_challenge,
    _private_key,
    _wallet_address,
)

DEFAULT_SCENARIO = "weather-now"
DEFAULT_NETWORKS = ["radius-testnet", "base-sepolia"]
MAX_CALLS = 100
MAX_CONCURRENCY = 25
DEFAULT_BUDGET = "0.05"
HTTP_TIMEOUT = 60


def _build_client(net_id: str):
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.http.x402_http_client import x402HTTPClientSync
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSignerWithRPC

    account = Account.from_key(_private_key())
    client = x402ClientSync()
    register_exact_evm_client(
        client, EthAccountSignerWithRPC(account, NETWORKS[net_id]["rpc_url"]), networks=net_id
    )
    return x402HTTPClientSync(client)


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return round(ordered[idx])


def _one_call(http, local: threading.local, url: str) -> dict:
    session = getattr(local, "session", None)
    if session is None:
        session = local.session = requests.Session()
    row: dict = {"started": time.time()}
    t0 = time.perf_counter()
    try:
        unpaid = session.get(url, timeout=HTTP_TIMEOUT)
        t1 = time.perf_counter()
        row["challenge_ms"] = round((t1 - t0) * 1000)
        if unpaid.status_code != 402:
            row.update(ok=False, error=f"expected 402, got {unpaid.status_code}")
            return row
        required = http.get_payment_required_response(lambda n: unpaid.headers.get(n), unpaid.content)
        payload = http.create_payment_payload(required)
        t2 = time.perf_counter()
        row["sign_ms"] = round((t2 - t1) * 1000)
        paid = session.get(url, headers=http.encode_payment_signature_header(payload), timeout=HTTP_TIMEOUT)
        t3 = time.perf_counter()
        row["paid_ms"] = round((t3 - t2) * 1000)
        row["total_ms"] = round((t3 - t0) * 1000)
        row["http_status"] = paid.status_code
        if paid.status_code == 200:
            try:
                settle = http.get_payment_settle_response(lambda n: paid.headers.get(n))
                row["tx_hash"] = settle.transaction or ""
            except Exception:  # noqa: BLE001 - a 200 without a receipt is still recorded
                row["tx_hash"] = ""
            row["ok"] = True
        else:
            try:
                detail = paid.json()
                reason = (detail.get("detail") or {}).get("invalidReason") or detail.get("error")
            except ValueError:
                reason = paid.text[:120]
            row.update(ok=False, error=f"{paid.status_code}: {reason}")
    except Exception as err:  # noqa: BLE001
        row.update(ok=False, error=f"{type(err).__name__}: {str(err)[:160]}")
        row.setdefault("total_ms", round((time.perf_counter() - t0) * 1000))
    return row


def _summarize(rows: list[dict], wall_s: float, net: dict, price: Decimal) -> dict:
    ok = [r for r in rows if r.get("ok")]
    errors: dict[str, int] = {}
    for r in rows:
        if not r.get("ok"):
            errors[r.get("error", "unknown")] = errors.get(r.get("error", "unknown"), 0) + 1
    paid = [r["paid_ms"] for r in ok if "paid_ms" in r]
    total = [r["total_ms"] for r in ok if "total_ms" in r]
    sign = [r["sign_ms"] for r in ok if "sign_ms" in r]
    return {
        "calls": len(rows),
        "succeeded": len(ok),
        "success_rate": round(len(ok) / len(rows), 3) if rows else 0,
        "paid_ms": {"p50": _pct(paid, 50), "p95": _pct(paid, 95), "max": max(paid) if paid else None,
                    "mean": round(statistics.mean(paid)) if paid else None},
        "total_ms": {"p50": _pct(total, 50), "p95": _pct(total, 95), "max": max(total) if total else None},
        "sign_ms_p50": _pct(sign, 50),
        "wall_s": round(wall_s, 2),
        "throughput_paid_per_s": round(len(ok) / wall_s, 2) if wall_s else None,
        "spent": format((price * len(ok)).normalize(), "f"),
        "asset": net["asset_symbol"],
        "sample_txs": [f"{net['explorer']}/tx/{r['tx_hash']}" for r in ok if r.get("tx_hash")][:3],
        "errors": errors,
    }


def x402_benchmark(params: dict) -> dict:
    networks = params.get("networks") or DEFAULT_NETWORKS
    if isinstance(networks, str):
        networks = [n.strip() for n in networks.split(",") if n.strip()]
    urls: dict = dict(params.get("urls") or {})
    scenario = str(params.get("scenario") or DEFAULT_SCENARIO).strip()
    calls = max(1, min(MAX_CALLS, int(params.get("calls") or 10)))
    levels = params.get("concurrency") or [1, 5]
    if isinstance(levels, (int, str)):
        levels = [levels]
    levels = sorted({max(1, min(MAX_CONCURRENCY, int(c))) for c in levels})
    budget = Decimal(str(params.get("budget") or DEFAULT_BUDGET))
    warmup = str(params.get("warmup", "true")).lower() not in {"false", "0", "no"}
    requested_by = str(params.get("requested_by") or "")

    # Resolve targets and quote each one unpaid before spending anything.
    targets = []
    for key in networks:
        net_id = NETWORK_BY_KEY.get(key, key)
        net = NETWORKS.get(net_id)
        if not net:
            raise X402Error(f"Unknown network {key!r}. Use: {sorted(NETWORK_BY_KEY)}.")
        url = urls.get(key) or urls.get(net_id)
        if not url:
            if not net.get("catalog_prefix"):
                raise X402Error(f"No demo endpoint for {net['label']}; pass urls={{'{key}': '<url>'}}.")
            url = f"{_demo_base_url()}{net['catalog_prefix']}/{scenario}"
        if not net["testnet"] and not _allow_mainnet():
            raise X402Error(f"{net['label']} is mainnet and X402_ALLOW_MAINNET is not enabled.")
        probe = requests.get(url, timeout=HTTP_TIMEOUT)
        challenge = _parse_challenge(probe) if probe.status_code == 402 else None
        if not challenge:
            raise X402Error(f"{url} did not return an x402 challenge (HTTP {probe.status_code}).")
        req = next((r for r in challenge.get("accepts") or [] if r.get("network") == net_id), None)
        if not req:
            raise X402Error(f"{url} does not offer payment on {net['label']}.")
        price = Decimal(_atomic_to_display(req["amount"], net["decimals"]))
        if price > _max_per_call():
            raise X402Error(f"{net['label']} price {price} {net['asset_symbol']} exceeds X402_MAX_PER_CALL {_max_per_call()}.")
        targets.append({"key": net["key"], "net_id": net_id, "net": net, "url": url, "price": price})

    paid_calls = calls * len(levels) + (1 if warmup else 0)
    estimate = sum(t["price"] for t in targets) * paid_calls
    if estimate > budget:
        raise X402Error(
            f"Estimated spend {estimate} (stablecoin units, all networks) exceeds budget {budget}. "
            f"Lower calls/concurrency or raise budget."
        )

    # Fail fast on an unfunded wallet: facilitators report it as an opaque verify error.
    payer = _wallet_address()
    for t in targets:
        need = t["price"] * paid_calls
        have = Decimal(_atomic_to_display(
            _erc20_balance(t["net"]["rpc_url"], t["net"]["asset"], payer), t["net"]["decimals"]))
        if have < need:
            raise X402Error(
                f"Wallet {payer} has {have} {t['net']['asset_symbol']} on {t['net']['label']}, "
                f"needs {need}. Fund it first (see x402_wallet_status funding hints)."
            )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    report: dict = {
        "run_id": run_id,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "client": "x402 python SDK (in-process, same path for every network)",
        "calls_per_level": calls,
        "concurrency_levels": levels,
        "budget": format(budget, "f"),
        "estimated_spend": format(estimate.normalize(), "f"),
        "results": {},
    }
    all_rows: list[dict] = []

    for target in targets:
        net, net_id, url = target["net"], target["net_id"], target["url"]
        http = _build_client(net_id)
        per_net: dict = {"url": url, "price": format(target["price"], "f"), "asset": net["asset_symbol"],
                         "network_label": net["label"]}
        if warmup:
            # First payment can include one-time work (allowance/permit, TLS, RPC warm-up); keep it out of stats.
            w = _one_call(http, threading.local(), url)
            per_net["warmup"] = {k: w.get(k) for k in ("ok", "total_ms", "error")}
            all_rows.append({**w, "network": net_id, "level": "warmup"})
        levels_out = {}
        for level in levels:
            local = threading.local()
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=level) as pool:
                rows = list(pool.map(lambda _i: _one_call(http, local, url), range(calls)))
            wall = time.perf_counter() - t0
            levels_out[f"c{level}"] = _summarize(rows, wall, net, target["price"])
            all_rows.extend({**r, "network": net_id, "level": level} for r in rows)
        per_net["levels"] = levels_out
        report["results"][target["key"]] = per_net

    # Paper trail: every payment attempt goes to the ledger, tagged with the run id.
    for r in all_rows:
        net = NETWORKS[r["network"]]
        target = next(t for t in targets if t["net_id"] == r["network"])
        _append_ledger({
            "id": str(uuid.uuid4()),
            "ts": datetime.fromtimestamp(r["started"], timezone.utc).isoformat(timespec="milliseconds"),
            "method": "GET", "url": target["url"], "memo": f"benchmark {run_id} (c={r['level']})",
            "requested_by": requested_by, "network": r["network"], "network_label": net["label"],
            "asset_symbol": net["asset_symbol"], "amount": format(target["price"], "f"),
            "outcome": "paid" if r.get("ok") else "payment_failed",
            "http_status": r.get("http_status"), "tx_hash": r.get("tx_hash", ""),
            "explorer_url": f"{net['explorer']}/tx/{r['tx_hash']}" if r.get("tx_hash") else "",
            "reason": r.get("error", ""), "latency_ms": r.get("total_ms"),
        })

    out_dir = Path(os.environ.get("X402_BENCHMARK_DIR", str(_ledger_path().parent / "benchmarks")))
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{run_id}.json"
    report_path.write_text(json.dumps({**report, "calls": all_rows}, indent=2, default=str))
    report["report_path"] = str(report_path)
    report["notes"] = [
        "paid_ms = server verify + facilitator settle + resource; it is the user-visible payment latency.",
        "Networks differ in facilitator as well as chain, so this measures each rail end to end, not raw block time.",
        "All payments come from one wallet; concurrency results include any same-payer contention in the facilitator.",
    ]
    return report
