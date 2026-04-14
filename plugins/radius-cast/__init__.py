import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_RPC_URL = "https://rpc.testnet.radiustech.xyz"
DEFAULT_CHAIN_ID = "72344"
DEFAULT_SBC_ADDRESS = "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb"
DEFAULT_EXPLORER_URL = "https://testnet.radiustech.xyz"
SBC_DECIMALS = 6
RUSD_DECIMALS = 18


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _app_root() -> Path:
    return Path(os.environ.get("HERMES_APP_ROOT", "/app"))


def _radius_dir() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    return Path(hermes_home) / ".radius"


def _private_key_file() -> Path:
    return _radius_dir() / "key"


def _address_file() -> Path:
    return _radius_dir() / "address"


def _radius_rpc_url() -> str:
    return os.environ.get("RADIUS_RPC_URL", DEFAULT_RPC_URL)


def _radius_chain_id() -> str:
    return str(os.environ.get("RADIUS_CHAIN_ID", DEFAULT_CHAIN_ID))


def _radius_sbc_address() -> str:
    return os.environ.get("RADIUS_SBC_ADDRESS", DEFAULT_SBC_ADDRESS)


def _radius_explorer_url() -> str:
    return os.environ.get("RADIUS_EXPLORER_URL", DEFAULT_EXPLORER_URL).rstrip("/")


def _script_fallback_enabled() -> bool:
    return os.environ.get("RADIUS_ALLOW_SCRIPT_FALLBACK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _format_units(value: int, decimals: int) -> str:
    negative = value < 0
    if negative:
        value = -value
    s = str(value).zfill(decimals)
    integer = s[:-decimals] if len(s) > decimals else "0"
    fraction = s[len(s) - decimals :].rstrip("0")
    sign = "-" if negative else ""
    return f"{sign}{integer}{f'.{fraction}' if fraction else ''}"


def _read_private_key() -> str:
    private_key = os.environ.get("RADIUS_PRIVATE_KEY", "").strip()
    if private_key:
        return private_key

    key_file = _private_key_file()
    if key_file.exists():
        return key_file.read_text().strip()

    raise RuntimeError(
        "No wallet configured. Set RADIUS_PRIVATE_KEY or initialize the Radius wallet first."
    )


def _read_address_hint() -> str:
    address = os.environ.get("RADIUS_WALLET_ADDRESS", "").strip()
    if address:
        return address

    address_file = _address_file()
    if address_file.exists():
        return address_file.read_text().strip()

    return ""


def _run_command(cmd, env=None):
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        message = stderr or stdout or f"Command failed with exit code {result.returncode}"
        raise RuntimeError(message)
    return (result.stdout or "").strip()


def _cast_env():
    env = os.environ.copy()
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)
    env.setdefault("NO_PROXY", "*")
    env.setdefault("no_proxy", "*")
    return env


def _run_cast(args):
    cast_bin = os.environ.get("RADIUS_CAST_BIN", "").strip()
    if not cast_bin:
        cast_bin = shutil.which("cast") or ""
    if not cast_bin:
        for candidate in (
            "/root/.foundry/bin/cast",
            "/usr/local/bin/cast",
            "/opt/foundry/bin/cast",
        ):
            if Path(candidate).exists():
                cast_bin = candidate
                break
    if not cast_bin:
        raise RuntimeError(
            "Foundry cast is not installed or not on PATH. Set RADIUS_CAST_BIN or install Foundry."
        )

    return _run_command([cast_bin, *args], env=_cast_env())


def _radius_script(name: str) -> str:
    for base_dir in (_app_root(), _repo_root()):
        script_path = base_dir / "scripts" / "radius" / name
        if script_path.exists():
            return str(script_path)
    return str(_app_root() / "scripts" / "radius" / name)


def _run_radius_script(name: str, args):
    return _run_command([sys.executable, _radius_script(name), *args])


def _parse_int(output: str) -> int:
    value = output.strip()
    if not value:
        raise ValueError("Expected integer output from cast, got empty string")

    first_token = value.split()[0]
    if first_token.startswith("0x"):
        return int(first_token, 16)

    match = re.match(r"^[+-]?\d+", first_token)
    if match:
        return int(match.group(0))

    raise ValueError(f"Could not parse integer from cast output: {output!r}")


def _resolve_wallet_address() -> str:
    address = _read_address_hint()
    if address:
        return address

    private_key = _read_private_key()
    return _run_cast(["wallet", "address", "--private-key", private_key]).strip()


def _cast_balance(address: str) -> dict:
    rusd_raw = _parse_int(
        _run_cast(["balance", address, "--rpc-url", _radius_rpc_url()])
    )
    sbc_raw = _parse_int(
        _run_cast(
            [
                "call",
                _radius_sbc_address(),
                "balanceOf(address)(uint256)",
                address,
                "--rpc-url",
                _radius_rpc_url(),
            ]
        )
    )
    return {
        "address": address,
        "rusd": _format_units(rusd_raw, RUSD_DECIMALS),
        "rusd_raw": str(rusd_raw),
        "sbc": _format_units(sbc_raw, SBC_DECIMALS),
        "sbc_raw": str(sbc_raw),
    }


def _balance(address: str) -> dict:
    try:
        result = _cast_balance(address)
        result["backend"] = "cast"
        return result
    except Exception as err:
        if not _script_fallback_enabled():
            raise RuntimeError(
                f"radius_balance failed via cast and script fallback is disabled: {err}"
            ) from err
        script_args = [address] if address else []
        data = json.loads(_run_radius_script("balance.py", script_args))
        data["backend"] = "script-fallback"
        data["cast_error"] = str(err)
        return data


def _cast_send_sbc(to_address: str, amount_sbc: str) -> dict:
    private_key = _read_private_key()
    from_address = _resolve_wallet_address()
    amount_raw = _run_cast(["parse-units", amount_sbc, str(SBC_DECIMALS)]).strip()

    balance = _cast_balance(from_address)
    if int(balance["sbc_raw"]) < int(amount_raw):
        raise RuntimeError(
            f"Insufficient SBC balance. Have {balance['sbc']}, need {amount_sbc}."
        )

    tx_hash = _run_cast(
        [
            "send",
            _radius_sbc_address(),
            "transfer(address,uint256)",
            to_address,
            amount_raw,
            "--rpc-url",
            _radius_rpc_url(),
            "--private-key",
            private_key,
            "--chain",
            _radius_chain_id(),
            "--legacy",
            "--async",
        ]
    ).strip()

    result = {
        "from": from_address,
        "to": to_address,
        "amount_sbc": amount_sbc,
        "amount_raw": amount_raw,
        "tx_hash": tx_hash,
        "status": "submitted",
        "explorer_url": f"{_radius_explorer_url()}/tx/{tx_hash}",
    }

    try:
        receipt_raw = _run_cast(
            [
                "receipt",
                tx_hash,
                "--rpc-url",
                _radius_rpc_url(),
                "--confirmations",
                "1",
                "--json",
            ]
        )
        receipt = json.loads(receipt_raw)
        result["block_number"] = str(
            receipt.get("blockNumber")
            or receipt.get("block_number")
            or receipt.get("block")
            or ""
        )
        status = receipt.get("status")
        if status in (1, "0x1", "1", True):
            result["status"] = "success"
        elif status in (0, "0x0", "0", False):
            result["status"] = "reverted"
    except Exception:
        pass

    return result


def _send_sbc(to_address: str, amount_sbc: str) -> dict:
    try:
        result = _cast_send_sbc(to_address, amount_sbc)
        result["backend"] = "cast"
        return result
    except Exception as err:
        if not _script_fallback_enabled():
            raise RuntimeError(
                f"radius_send_sbc failed via cast and script fallback is disabled: {err}"
            ) from err
        data = json.loads(_run_radius_script("send.py", [to_address, amount_sbc]))
        data["backend"] = "script-fallback"
        data["cast_error"] = str(err)
        data["explorer_url"] = f"{_radius_explorer_url()}/tx/{data['tx_hash']}"
        return data


def _tx_status(tx_hash: str) -> dict:
    receipt_raw = _run_cast(
        [
            "receipt",
            tx_hash,
            "--rpc-url",
            _radius_rpc_url(),
            "--json",
        ]
    )
    try:
        receipt = json.loads(receipt_raw)
    except json.JSONDecodeError:
        return {"tx_hash": tx_hash, "raw": receipt_raw}

    return {
        "tx_hash": tx_hash,
        "block_number": str(
            receipt.get("blockNumber")
            or receipt.get("block_number")
            or receipt.get("block")
            or ""
        ),
        "status": receipt.get("status"),
        "backend": "cast",
        "raw": receipt,
    }


def register(ctx):
    def radius_wallet_address(params, **kwargs):
        result = {"address": _resolve_wallet_address()}
        return json.dumps(result)

    def radius_balance(params, **kwargs):
        address = (params or {}).get("address") or _resolve_wallet_address()
        return json.dumps(_balance(address))

    def radius_send_sbc(params, **kwargs):
        to_address = (params or {}).get("to")
        amount_sbc = str((params or {}).get("amount_sbc", "")).strip()
        if not to_address:
            return "Error: missing required parameter 'to'."
        if not amount_sbc:
            return "Error: missing required parameter 'amount_sbc'."
        return json.dumps(_send_sbc(to_address, amount_sbc))

    def radius_tx_status(params, **kwargs):
        tx_hash = (params or {}).get("tx_hash")
        if not tx_hash:
            return "Error: missing required parameter 'tx_hash'."
        return json.dumps(_tx_status(tx_hash))

    ctx.register_tool(
        name="radius_wallet_address",
        toolset="radius-cast",
        schema={
            "name": "radius_wallet_address",
            "description": (
                "Return this agent's Radius wallet address. Uses cast wallet derivation with the "
                "configured Radius wallet key."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=radius_wallet_address,
    )

    ctx.register_tool(
        name="radius_balance",
        toolset="radius-cast",
        schema={
            "name": "radius_balance",
            "description": (
                "Get Radius balances for an address. Returns Radius Testnet RUSD native balance "
                "and SBC ERC-20 balance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": (
                            "Optional address to inspect. If omitted, the agent wallet address is used."
                        ),
                    }
                },
                "required": [],
            },
        },
        handler=radius_balance,
    )

    ctx.register_tool(
        name="radius_send_sbc",
        toolset="radius-cast",
        schema={
            "name": "radius_send_sbc",
            "description": (
                "Send SBC on Radius Testnet to a recipient address. Uses cast send with "
                "Radius-specific defaults and returns the tx hash plus explorer URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient EVM address.",
                    },
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
        toolset="radius-cast",
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
