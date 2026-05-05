#!/usr/bin/env python3
"""
Radius wallet init helper.

Boot flow expectations in this template:
- Wallet is instantiated by radius-cli (local keystore under RADIUS_HOME).
- This helper optionally requests testnet faucet funding for that wallet.
- If needed by downstream auth components, it can export private key from radius-cli
  and place it into process env (caller persists it if desired).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chain import SBC_ADDRESS, SBC_DECIMALS, ERC20_ABI, FAUCET_BASE, format_units, create_web3


def _radius_home() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    return Path(os.environ.get("RADIUS_HOME", str(Path(hermes_home) / ".radius-cli")))


def _radius_cli() -> str:
    return os.environ.get("RADIUS_CLI_BIN", "radius-cli")


def _run(cmd: list[str], *, input_text: str | None = None) -> str:
    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        env={**os.environ, "RADIUS_HOME": str(_radius_home())},
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(msg or f"command failed: {' '.join(cmd)}")
    return (proc.stdout or "").strip()


def _wallet_address() -> str:
    out = _run(
        [
            _radius_cli(),
            "--network",
            os.environ.get("RADIUS_NETWORK", "testnet"),
            "--rpc-url",
            os.environ.get("RADIUS_RPC_URL", "https://rpc.testnet.radiustech.xyz"),
            "--sbc",
            os.environ.get("RADIUS_SBC_ADDRESS", SBC_ADDRESS),
            "wallet",
            "address",
        ]
    )
    match = re.search(r"0x[a-fA-F0-9]{40}", out)
    if not match:
        raise RuntimeError("could not parse wallet address from radius-cli output")
    return match.group(0)


def _export_private_key() -> str:
    out = _run([_radius_cli(), "wallet", "export"], input_text="y\n")
    match = re.search(r"0x[a-fA-F0-9]{64}", out)
    if not match:
        raise RuntimeError("could not parse private key from radius-cli export output")
    return match.group(0)


def _persist_address(address: str) -> None:
    radius_home = _radius_home()
    radius_home.mkdir(parents=True, exist_ok=True)
    (radius_home / "address").write_text(address)


def get_challenge(addr: str) -> str:
    res = requests.get(f"{FAUCET_BASE}/challenge/{addr}", params={"token": "***"}, timeout=15)
    res.raise_for_status()
    data = res.json()
    return data.get("message") or data.get("challenge", "")


def sign_message(private_key: str, message: str) -> str:
    account = Account.from_key(private_key)
    msg = encode_defunct(text=message)
    signed = account.sign_message(msg)
    return "0x" + signed.signature.hex()


def drip_with_signature(addr: str, private_key: str) -> dict:
    message = get_challenge(addr)
    signature = sign_message(private_key, message)
    res = requests.post(
        f"{FAUCET_BASE}/drip",
        json={"address": addr, "token": "***", "signature": signature},
        timeout=15,
    )
    data = res.json()
    if not res.ok:
        raise RuntimeError(data.get("error") or data.get("message") or json.dumps(data))
    return data


def drip(addr: str, private_key: str):
    res = requests.post(
        f"{FAUCET_BASE}/drip",
        json={"address": addr, "token": "***"},
        timeout=15,
    )
    data = res.json()
    if res.ok:
        return data

    err_code = data.get("error", "")
    if err_code == "signature_required" or res.status_code == 401:
        print("[radius] Faucet requires signed request, signing challenge...")
        return drip_with_signature(addr, private_key)
    if err_code == "rate_limited":
        retry_ms = data.get("retry_after_ms") or (data.get("retry_after_seconds", 0) * 1000)
        print(f"[radius] Faucet rate-limited. Retry after {int(retry_ms / 1000)}s.")
        return None
    raise RuntimeError(data.get("error") or data.get("message") or json.dumps(data))


def get_sbc_balance(addr: str, private_key: str) -> int:
    w3, _ = create_web3(private_key)
    contract = w3.eth.contract(address=Web3.to_checksum_address(SBC_ADDRESS), abi=ERC20_ABI)
    return contract.functions.balanceOf(Web3.to_checksum_address(addr)).call()


def main() -> int:
    try:
        address = _wallet_address()
        _persist_address(address)
        print(f"[radius] Wallet address: {address}")

        # Make private key available to caller via env for auth/JWT/registry code paths.
        try:
            private_key = _export_private_key()
            os.environ["RADIUS_PRIVATE_KEY"] = private_key
        except Exception as err:
            print(f"[radius] WARNING: could not export private key from radius-cli: {err}")
            private_key = ""

        auto_fund = os.environ.get("RADIUS_AUTO_FUND", "")
        if auto_fund in ("false", "0"):
            print("[radius] RADIUS_AUTO_FUND disabled, skipping faucet.")
            return 0

        if not private_key:
            print("[radius] Skipping faucet: private key not available for challenge signing.")
            return 0

        print("[radius] Requesting SBC from faucet...")
        result = drip(address, private_key)
        if result:
            tx_hash = result.get("tx_hash") or result.get("txHash") or result.get("hash", "")
            if tx_hash:
                print(f"[radius] Faucet tx: {tx_hash}")
            print("[radius] Faucet request submitted. Waiting for balance...")
            for _ in range(5):
                time.sleep(3)
                try:
                    bal = get_sbc_balance(address, private_key)
                    if bal > 0:
                        print(f"[radius] SBC balance: {format_units(bal, SBC_DECIMALS)} SBC")
                        break
                except Exception:
                    pass

        print("[radius] Wallet initialization complete.")
        return 0
    except Exception as err:
        print(f"[radius] Wallet initialization failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
