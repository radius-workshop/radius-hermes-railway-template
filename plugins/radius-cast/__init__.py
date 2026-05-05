from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

DEFAULT_RPC_URL = "https://rpc.testnet.radiustech.xyz"
DEFAULT_NETWORK = "testnet"
DEFAULT_CHAIN_ID = "72344"
DEFAULT_SBC_ADDRESS = "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb"
DEFAULT_EXPLORER_URL = "https://testnet.radiustech.xyz"


class RadiusCliError(RuntimeError):
    pass


def _radius_home() -> str:
    # Keep radius-cli keystore/config under Hermes state so wallet persists across deploys.
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    return os.environ.get("RADIUS_HOME", str(Path(hermes_home) / ".radius-cli"))


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["RADIUS_HOME"] = _radius_home()
    return env


def _radius_cli_cmd() -> list[str]:
    configured = os.environ.get("RADIUS_CLI_BIN", "").strip()
    if configured:
        return shlex.split(configured)

    resolved = shutil.which("radius-cli")
    if resolved:
        return [resolved]

    # Fallback when global npm bin is not on PATH.
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "radius-cli"]

    raise RadiusCliError(
        "radius-cli is not installed or not on PATH. Install it (for example: npm install -g radius-cli)."
    )


def _network() -> str:
    return os.environ.get("RADIUS_NETWORK", DEFAULT_NETWORK)


def _rpc_url() -> str:
    return os.environ.get("RADIUS_RPC_URL", DEFAULT_RPC_URL)


def _sbc_address() -> str:
    return os.environ.get("RADIUS_SBC_ADDRESS", DEFAULT_SBC_ADDRESS)


def _explorer_url() -> str:
    return os.environ.get("RADIUS_EXPLORER_URL", DEFAULT_EXPLORER_URL).rstrip("/")


def _chain_id() -> str:
    return str(os.environ.get("RADIUS_CHAIN_ID", DEFAULT_CHAIN_ID))


def _run(args: list[str]) -> str:
    cmd = [*_radius_cli_cmd(), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_env())
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RadiusCliError(msg or f"radius-cli failed with exit code {proc.returncode}")
    return (proc.stdout or "").strip()


def _common_args(*, json_output: bool = False) -> list[str]:
    args = [
        "--network",
        _network(),
        "--rpc-url",
        _rpc_url(),
        "--sbc",
        _sbc_address(),
    ]
    if json_output:
        args.append("--json")
    return args


def _extract_first_address(text: str) -> str:
    match = re.search(r"0x[a-fA-F0-9]{40}", text)
    if not match:
        raise RadiusCliError("Could not parse wallet address from radius-cli output")
    return match.group(0)


def _extract_tx_hash(text: str) -> str:
    match = re.search(r"0x[a-fA-F0-9]{64}", text)
    if not match:
        raise RadiusCliError("Could not parse transaction hash from radius-cli output")
    return match.group(0)


def _wallet_address() -> dict:
    out = _run([*_common_args(), "wallet", "address"])
    return {
        "address": _extract_first_address(out),
        "provider": "local",
        "backend": "radius-cli",
    }


def _balance(address: str | None = None) -> dict:
    cmd = [*_common_args(json_output=True), "wallet", "balance"]
    if address:
        cmd.append(address)
    out = _run(cmd)
    data = json.loads(out)
    data["provider"] = "local"
    data["backend"] = "radius-cli"
    return data


def _send_sbc(to_address: str, amount_sbc: str) -> dict:
    cmd = [*_common_args(json_output=True), "wallet", "send", to_address, amount_sbc, "SBC"]
    out = _run(cmd)

    tx_hash = ""
    payload: dict = {}
    try:
        parsed = json.loads(out)
        if isinstance(parsed, dict):
            payload = parsed
            tx_hash = str(
                parsed.get("tx_hash")
                or parsed.get("txHash")
                or parsed.get("transactionHash")
                or parsed.get("hash")
                or ""
            ).strip()
    except json.JSONDecodeError:
        pass

    if not tx_hash:
        tx_hash = _extract_tx_hash(out)

    result = {
        "from": _wallet_address()["address"],
        "to": to_address,
        "amount_sbc": amount_sbc,
        "tx_hash": tx_hash,
        "status": "submitted",
        "provider": "local",
        "backend": "radius-cli",
        "explorer_url": f"{_explorer_url()}/tx/{tx_hash}",
    }
    if payload:
        result["raw"] = payload
    return result


def _tx_status(tx_hash: str) -> dict:
    raw: dict | str
    try:
        out = _run([*_common_args(json_output=True), "receipt", tx_hash])
        raw = json.loads(out)
    except Exception as json_err:
        try:
            out = _run([*_common_args(), "receipt", tx_hash])
            raw = out
        except Exception as err:
            return {
                "tx_hash": tx_hash,
                "block_number": "",
                "status": "pending_or_not_found",
                "backend": "radius-cli",
                "chain_id": _chain_id(),
                "error": str(err),
                "json_error": str(json_err),
            }

    status = None
    block_number = ""
    if isinstance(raw, dict):
        status = raw.get("status")
        block_number = str(raw.get("blockNumber") or raw.get("block_number") or raw.get("block") or "")

    return {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "status": status,
        "backend": "radius-cli",
        "chain_id": _chain_id(),
        "raw": raw,
    }


def register(ctx):
    def radius_wallet_address(_params, **_kwargs):
        return json.dumps(_wallet_address())

    def radius_balance(params, **_kwargs):
        params = params or {}
        address = str(params.get("address") or "").strip() or None
        return json.dumps(_balance(address))

    def radius_send_sbc(params, **_kwargs):
        params = params or {}
        to_address = str(params.get("to") or "").strip()
        amount_sbc = str(params.get("amount_sbc") or "").strip()
        if not to_address:
            return "Error: missing required parameter 'to'."
        if not amount_sbc:
            return "Error: missing required parameter 'amount_sbc'."
        return json.dumps(_send_sbc(to_address, amount_sbc))

    def radius_tx_status(params, **_kwargs):
        params = params or {}
        tx_hash = str(params.get("tx_hash") or "").strip()
        if not tx_hash:
            return "Error: missing required parameter 'tx_hash'."
        return json.dumps(_tx_status(tx_hash))

    ctx.register_tool(
        name="radius_wallet_address",
        toolset="radius-cli",
        schema={
            "name": "radius_wallet_address",
            "description": "Return this agent's Radius wallet address from the local radius-cli keystore.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=radius_wallet_address,
    )

    ctx.register_tool(
        name="radius_balance",
        toolset="radius-cli",
        schema={
            "name": "radius_balance",
            "description": (
                "Get Radius balances for an address. Returns Radius Testnet RUSD native balance "
                "and SBC ERC-20 balance. If address is omitted, the local radius-cli wallet is used."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Optional address to inspect. Defaults to the local radius-cli wallet.",
                    }
                },
                "required": [],
            },
        },
        handler=radius_balance,
    )

    ctx.register_tool(
        name="radius_send_sbc",
        toolset="radius-cli",
        schema={
            "name": "radius_send_sbc",
            "description": "Send SBC on Radius to a recipient address using the local radius-cli wallet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient EVM address."},
                    "amount_sbc": {
                        "type": "string",
                        "description": "Decimal SBC amount to send, for example '1.25'.",
                    },
                },
                "required": ["to", "amount_sbc"],
            },
        },
        handler=radius_send_sbc,
    )

    ctx.register_tool(
        name="radius_tx_status",
        toolset="radius-cli",
        schema={
            "name": "radius_tx_status",
            "description": "Fetch a Radius transaction receipt by hash.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tx_hash": {
                        "type": "string",
                        "description": "Transaction hash to inspect.",
                    }
                },
                "required": ["tx_hash"],
            },
        },
        handler=radius_tx_status,
    )
