"""Sequential bounded requests through the exact same CLI and ledger path as tools."""
from decimal import Decimal, ROUND_DOWN
import time
from . import X402Error, _decimal, _max_per_call, x402_request


def x402_benchmark(params):
    calls = params.get("calls", 5)
    if type(calls) is not int or not 1 <= calls <= 20:
        raise X402Error("calls must be an integer from 1 to 20")
    if params.get("networks") or params.get("concurrency"):
        raise X402Error("cross-network and concurrent benchmarks are disabled")
    budget = _decimal(params.get("budget", "0.01"), "budget")
    if budget > Decimal("0.05"):
        raise X402Error("benchmark budget cannot exceed 0.05 SBC")
    cap = min(_max_per_call(), (budget / calls).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))
    if cap <= 0:
        raise X402Error("budget is too small for the requested call count")
    rows = []
    for _ in range(calls):
        started = time.perf_counter()
        result = x402_request({"url": params.get("url", ""), "max_amount": format(cap, "f"),
                               "memo": "bounded sequential benchmark", "requested_by": params.get("requested_by", "")})
        rows.append({**result, "total_ms": round((time.perf_counter() - started) * 1000)})
        if result.get("outcome") != "settlement_reported" or result.get("ledger_error"):
            break
    return {"calls_requested": calls, "calls_attempted": len(rows), "authorization_bound_sbc": format(cap * calls, "f"),
            "results": rows, "notes": ["End-to-end client latency includes CLI startup and challenge requests.",
                                       "Per-run bound only; no cumulative wallet budget or chain reconciliation."]}
