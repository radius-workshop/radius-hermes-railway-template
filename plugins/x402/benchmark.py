"""Sequential bounded requests through the exact same CLI and ledger path as tools."""
from decimal import Decimal, ROUND_DOWN
import time
from . import X402Error, NETWORKS, _decimal, _max_per_call, _network_name, _enabled_networks, x402_request


def x402_benchmark(params):
    calls = params.get("calls", 5)
    if type(calls) is not int or not 1 <= calls <= 20:
        raise X402Error("calls must be an integer from 1 to 20")
    if params.get("networks") or params.get("concurrency"):
        raise X402Error("cross-network and concurrent benchmarks are disabled")
    budget = _decimal(params.get("budget", "0.01"), "budget")
    if budget > Decimal("0.05"):
        raise X402Error("benchmark budget cannot exceed 0.05 units of the selected network's payment token")
    cap = (budget / calls).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    operator_cap = _max_per_call()
    if operator_cap is not None:
        cap = min(operator_cap, cap)
    if cap <= 0:
        raise X402Error("budget is too small for the requested call count")
    rows = []
    # A benchmark stays on one rail; do not silently compare different currencies
    # or environments when the seller changes offer ordering between requests.
    network = _network_name(params["network"]) if params.get("network") else _enabled_networks()[0]
    for _ in range(calls):
        started = time.perf_counter()
        result = x402_request({"url": params.get("url", ""), "network": network, "max_amount": format(cap, "f"),
                               "memo": "bounded sequential benchmark", "requested_by": params.get("requested_by", "")})
        rows.append({**result, "total_ms": round((time.perf_counter() - started) * 1000)})
        if result.get("outcome") != "settlement_reported" or result.get("ledger_error"):
            break
    output = {"calls_requested": calls, "calls_attempted": len(rows), "authorization_bound": format(cap * calls, "f"),
            "network": NETWORKS[network]["network"], "asset_symbol": NETWORKS[network]["symbol"],
            "results": rows, "notes": ["End-to-end client latency includes CLI startup and challenge requests.",
                                       "Per-run bound only; no cumulative wallet budget or chain reconciliation."]}
    if NETWORKS[network]["symbol"] == "SBC":
        output["authorization_bound_sbc"] = output["authorization_bound"]
    return output
