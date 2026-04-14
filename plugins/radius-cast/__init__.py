from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_state import (
    current_session_id,
    get_session_provider,
    get_session_record,
    normalize_provider,
    post_tool_call,
    pre_tool_call,
    resolve_provider,
    set_session_provider,
    set_session_provider_error,
)


DEFAULT_RPC_URL = "https://rpc.testnet.radiustech.xyz"
DEFAULT_CHAIN_ID = "72344"
DEFAULT_SBC_ADDRESS = "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb"
DEFAULT_EXPLORER_URL = "https://testnet.radiustech.xyz"
SBC_DECIMALS = 6
RUSD_DECIMALS = 18

ERC20_ABI = [
    {
        "type": "function",
        "name": "balanceOf",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "transfer",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
]

_PARA_WALLET_CACHE: dict[str, object] = {"value": None, "expires_at": 0.0}


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


def _radius_chain_id_int() -> int:
    return int(_radius_chain_id())


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


def _create_web3() -> Web3:
    from web3 import Web3
    return Web3(Web3.HTTPProvider(_radius_rpc_url()))


def _erc20_contract(w3: Web3):
    from web3 import Web3
    return w3.eth.contract(
        address=Web3.to_checksum_address(_radius_sbc_address()),
        abi=ERC20_ABI,
    )


def _provider_error(provider: str, message: str) -> RuntimeError:
    return RuntimeError(f"{provider} wallet provider unavailable: {message}")


def _resolve_wallet_address_local() -> str:
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


def _balance_local(address: str) -> dict:
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


def _send_sbc_local(to_address: str, amount_sbc: str) -> dict:
    try:
        private_key = _read_private_key()
        from_address = _resolve_wallet_address_local()
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
            "backend": "cast",
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


def _para_base_url() -> str:
    explicit = str(os.environ.get("PARA_REST_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    env = str(os.environ.get("PARA_ENVIRONMENT") or "beta").strip().lower()
    if env in {"prod", "production"}:
        return "https://api.getpara.com"
    return "https://api.beta.getpara.com"


def _para_api_key() -> str:
    for key in ("PARA_API_KEY", "PARA_SECRET_API_KEY"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    raise _provider_error("para", "set PARA_API_KEY for Para REST access")


def _para_headers() -> dict[str, str]:
    return {
        "X-API-Key": _para_api_key(),
        "Content-Type": "application/json",
    }


def _para_request(method: str, path: str, payload: dict | None = None) -> dict | list:
    url = f"{_para_base_url()}{path}"
    response = requests.request(
        method,
        url,
        headers=_para_headers(),
        json=payload,
        timeout=30,
    )
    try:
        data = response.json()
    except Exception:
        data = None
    if not response.ok:
        message = None
        if isinstance(data, dict):
            message = data.get("message") or data.get("code")
        raise _provider_error("para", message or f"{response.status_code} {response.text.strip()}")
    return data if data is not None else {}


def _coerce_wallet_list(data: dict | list) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("wallets", "items", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _wallet_type(wallet: dict) -> str:
    return str(wallet.get("type") or wallet.get("walletType") or "").strip().lower()


def _wallet_id(wallet: dict) -> str:
    return str(wallet.get("id") or wallet.get("walletId") or "").strip()


def _wallet_address(wallet: dict) -> str:
    for key in ("address", "walletAddress", "publicAddress"):
        value = str(wallet.get(key) or "").strip()
        if value:
            return value
    return ""


def _cached_para_wallet() -> dict | None:
    value = _PARA_WALLET_CACHE.get("value")
    expires_at = float(_PARA_WALLET_CACHE.get("expires_at") or 0.0)
    if value and time.time() < expires_at:
        return value if isinstance(value, dict) else None
    return None


def _store_para_wallet(wallet: dict) -> dict:
    _PARA_WALLET_CACHE["value"] = wallet
    _PARA_WALLET_CACHE["expires_at"] = time.time() + 30.0
    return wallet


def _resolve_para_wallet() -> dict:
    cached = _cached_para_wallet()
    if cached:
        return cached

    explicit_wallet_id = str(os.environ.get("PARA_WALLET_ID") or "").strip()
    if explicit_wallet_id:
        wallet = _para_request("GET", f"/v1/wallets/{explicit_wallet_id}")
        if not isinstance(wallet, dict):
            raise _provider_error("para", "unexpected wallet payload from Para")
        wallet_id = _wallet_id(wallet)
        address = _wallet_address(wallet)
        if not wallet_id or not address:
            raise _provider_error("para", "wallet payload missing id or address")
        return _store_para_wallet({"id": wallet_id, "address": address, "type": _wallet_type(wallet)})

    wallets = _coerce_wallet_list(_para_request("GET", "/v1/wallets"))
    evm_wallets = [wallet for wallet in wallets if _wallet_type(wallet) == "evm"]
    if not evm_wallets:
        raise _provider_error("para", "no EVM wallet found in the configured Para project")
    if len(evm_wallets) > 1:
        raise _provider_error(
            "para",
            "multiple EVM wallets found; set PARA_WALLET_ID to choose the operator wallet",
        )

    wallet = evm_wallets[0]
    wallet_id = _wallet_id(wallet)
    address = _wallet_address(wallet)
    if not wallet_id or not address:
        raise _provider_error("para", "wallet payload missing id or address")
    return _store_para_wallet({"id": wallet_id, "address": address, "type": _wallet_type(wallet)})


def _ensure_hex_prefixed(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise _provider_error("para", "missing signed transaction payload")
    return text if text.startswith("0x") else f"0x{text}"


def _extract_signed_transaction(payload: dict | list) -> str:
    if isinstance(payload, dict):
        for key in ("transactionData", "signedTransaction", "signature", "data"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _ensure_hex_prefixed(value)
        inner = payload.get("result")
        if isinstance(inner, dict):
            return _extract_signed_transaction(inner)
    raise _provider_error("para", "sign-transaction response did not include a signed transaction")


def _broadcast_raw_transaction(raw_tx: str) -> str:
    response = requests.post(
        _radius_rpc_url(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_sendRawTransaction",
            "params": [raw_tx],
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        message = data["error"].get("message") or json.dumps(data["error"])
        raise RuntimeError(f"Radius RPC broadcast failed: {message}")
    tx_hash = str(data.get("result") or "").strip()
    if not tx_hash:
        raise RuntimeError("Radius RPC broadcast did not return a transaction hash")
    return tx_hash


def _wait_for_receipt(w3: Web3, tx_hash: str, timeout: float = 30.0):
    try:
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout, poll_latency=1.0)
    except Exception:
        return None


def _para_wallet_address() -> dict:
    wallet = _resolve_para_wallet()
    return {
        "address": wallet["address"],
        "wallet_id": wallet["id"],
        "provider": "para",
        "backend": "para-rest",
    }


def _balance_para(address: str) -> dict:
    result = _cast_balance(address)
    result["backend"] = "para-rest"
    result["provider"] = "para"
    return result


def _send_sbc_para(to_address: str, amount_sbc: str) -> dict:
    from web3 import Web3
    from web3.exceptions import ContractLogicError

    wallet = _resolve_para_wallet()
    from_address = str(wallet["address"])
    wallet_id = str(wallet["id"])
    amount_raw_int = int(_run_cast(["parse-units", amount_sbc, str(SBC_DECIMALS)]).strip())

    balance = _balance_para(from_address)
    if int(balance["sbc_raw"]) < amount_raw_int:
        raise RuntimeError(
            f"Insufficient SBC balance. Have {balance['sbc']}, need {amount_sbc}."
        )

    w3 = _create_web3()
    contract = _erc20_contract(w3)
    checksum_from = Web3.to_checksum_address(from_address)
    checksum_to = Web3.to_checksum_address(to_address)
    tx_data = contract.functions.transfer(checksum_to, amount_raw_int)._encode_transaction_data()
    gas_price = int(w3.eth.gas_price)
    nonce = int(w3.eth.get_transaction_count(checksum_from))
    try:
        gas_estimate = int(
            w3.eth.estimate_gas(
                {
                    "from": checksum_from,
                    "to": Web3.to_checksum_address(_radius_sbc_address()),
                    "data": tx_data,
                    "value": 0,
                }
            )
        )
    except ContractLogicError as err:
        message = str(err)
        if "transfer amount exceeds balance" in message.lower():
            raise RuntimeError(
                f"Insufficient SBC balance. Have {balance['sbc']}, need {amount_sbc}."
            ) from err
        raise
    gas_limit = max(gas_estimate + 10_000, int(gas_estimate * 1.2))

    sign_payload = {
        "transaction": {
            "to": Web3.to_checksum_address(_radius_sbc_address()),
            "value": 0,
            "gasLimit": gas_limit,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": _radius_chain_id_int(),
            "data": tx_data,
            "type": 0,
        },
        "chainId": _radius_chain_id_int(),
    }
    signed = _extract_signed_transaction(
        _para_request("POST", f"/v1/wallets/{wallet_id}/sign-transaction", sign_payload)
    )
    tx_hash = _broadcast_raw_transaction(signed)

    result = {
        "from": from_address,
        "to": to_address,
        "amount_sbc": amount_sbc,
        "amount_raw": str(amount_raw_int),
        "tx_hash": tx_hash,
        "status": "submitted",
        "explorer_url": f"{_radius_explorer_url()}/tx/{tx_hash}",
        "backend": "para-rest",
        "provider": "para",
        "wallet_id": wallet_id,
    }

    receipt = _wait_for_receipt(w3, tx_hash)
    if receipt is not None:
        result["block_number"] = str(receipt.get("blockNumber") or "")
        status = receipt.get("status")
        if status in (1, "0x1", "1", True):
            result["status"] = "success"
        elif status in (0, "0x0", "0", False):
            result["status"] = "reverted"

    return result


def _is_para_available() -> tuple[bool, str]:
    try:
        wallet = _resolve_para_wallet()
        return True, f"wallet {wallet['address']}"
    except Exception as err:
        return False, str(err)


def _parse_provider_switch_request(message: str) -> str | None:
    text = str(message or "").strip().lower()
    if not text:
        return None
    patterns = (
        r"\buse\s+(para|local)\s+wallet\b",
        r"\buse\s+(para|local)\s+as\s+(?:the\s+)?wallet\b",
        r"\bswitch(?:\s+back)?\s+to\s+(para|local)\s+wallet\b",
        r"\bset\s+(?:the\s+)?wallet\s+provider\s+to\s+(para|local)\b",
        r"\bchange\s+(?:the\s+)?wallet\s+provider\s+to\s+(para|local)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _provider_status_context(provider: str, session_id: str) -> str:
    record = get_session_record(session_id)
    error_line = ""
    if record.get("error"):
        error_line = f"- provider error: {record['error']}\n"
    return (
        f"Wallet provider status for this session:\n"
        f"- session_id: {session_id}\n"
        f"- default wallet provider: {provider}\n"
        f"{error_line}"
        f"- For Radius wallet tools, omit the provider argument when the user wants the session default.\n"
        f"- If the user explicitly asks for the local or para wallet in this turn, pass the provider argument "
        f"to `radius_wallet_address`, `radius_balance`, or `radius_send_sbc`."
    )


class LocalWalletBackend:
    provider = "local"

    def wallet_address(self) -> dict:
        return {
            "address": _resolve_wallet_address_local(),
            "provider": "local",
            "backend": "cast",
        }

    def balance(self, address: str | None = None) -> dict:
        target = address or _resolve_wallet_address_local()
        data = _balance_local(target)
        data["provider"] = "local"
        return data

    def send_sbc(self, to_address: str, amount_sbc: str) -> dict:
        data = _send_sbc_local(to_address, amount_sbc)
        data["provider"] = "local"
        return data


class ParaWalletBackend:
    provider = "para"

    def wallet_address(self) -> dict:
        return _para_wallet_address()

    def balance(self, address: str | None = None) -> dict:
        target = address or str(_resolve_para_wallet()["address"])
        return _balance_para(target)

    def send_sbc(self, to_address: str, amount_sbc: str) -> dict:
        return _send_sbc_para(to_address, amount_sbc)


def _backend_for_provider(provider: str):
    normalized = normalize_provider(provider)
    if normalized == "para":
        return ParaWalletBackend()
    return LocalWalletBackend()


def _effective_provider(params: dict | None, kwargs: dict | None = None) -> tuple[str, str]:
    params = params or {}
    kwargs = kwargs or {}
    session_id = current_session_id(kwargs)
    provider = resolve_provider(params.get("provider"), session_id=session_id)
    return provider, session_id


def register(ctx):
    def radius_pre_llm_call(session_id: str, user_message: str, **kwargs):
        switch_to = _parse_provider_switch_request(user_message)
        if switch_to:
            if switch_to == "para":
                available, reason = _is_para_available()
                if not available:
                    set_session_provider_error(session_id, "para", reason)
                    return {
                        "context": (
                            f"{_provider_status_context('para', session_id)}\n\n"
                            f"The user explicitly requested the para wallet for this session, but the para provider "
                            f"is unavailable: {reason}\n"
                            f"Treat this as a hard error. Do not silently fall back to the local wallet for default "
                            f"wallet actions in this session. Only use the local wallet if the user explicitly asks "
                            f"for it in this turn or explicitly switches the session back to local."
                        )
                    }
            provider = set_session_provider(session_id, switch_to)
            return {
                "context": (
                    f"{_provider_status_context(provider, session_id)}\n\n"
                    f"The user explicitly switched the session wallet provider to {provider}. "
                    f"Confirm the change in your response and use this provider as the default for wallet actions "
                    f"unless the user explicitly asks for the other provider in this turn."
                )
            }

        return {"context": _provider_status_context(get_session_provider(session_id), session_id)}

    def radius_on_session_start(session_id: str, **kwargs):
        set_session_provider(session_id, "local")

    def radius_wallet_address(params, **kwargs):
        provider, session_id = _effective_provider(params, kwargs)
        data = _backend_for_provider(provider).wallet_address()
        data["session_id"] = session_id or None
        return json.dumps(data)

    def radius_balance(params, **kwargs):
        provider, session_id = _effective_provider(params, kwargs)
        address = str((params or {}).get("address") or "").strip() or None
        data = _backend_for_provider(provider).balance(address)
        data["session_id"] = session_id or None
        return json.dumps(data)

    def radius_send_sbc(params, **kwargs):
        provider, session_id = _effective_provider(params, kwargs)
        to_address = str((params or {}).get("to") or "").strip()
        amount_sbc = str((params or {}).get("amount_sbc", "")).strip()
        if not to_address:
            return "Error: missing required parameter 'to'."
        if not amount_sbc:
            return "Error: missing required parameter 'amount_sbc'."
        data = _backend_for_provider(provider).send_sbc(to_address, amount_sbc)
        data["session_id"] = session_id or None
        return json.dumps(data)

    def radius_tx_status(params, **kwargs):
        tx_hash = (params or {}).get("tx_hash")
        if not tx_hash:
            return "Error: missing required parameter 'tx_hash'."
        return json.dumps(_tx_status(tx_hash))

    ctx.register_hook("pre_llm_call", radius_pre_llm_call)
    ctx.register_hook("on_session_start", radius_on_session_start)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)

    ctx.register_tool(
        name="radius_wallet_address",
        toolset="radius-cast",
        schema={
            "name": "radius_wallet_address",
            "description": (
                "Return this agent's Radius wallet address for the selected provider. "
                "Defaults to the session wallet provider and supports explicit provider overrides."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["local", "para"],
                        "description": (
                            "Optional wallet provider override. If omitted, the session wallet provider is used."
                        ),
                    }
                },
                "required": [],
            },
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
                "and SBC ERC-20 balance. If address is omitted, the selected provider wallet is used."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": (
                            "Optional address to inspect. If omitted, the selected provider wallet address is used."
                        ),
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["local", "para"],
                        "description": (
                            "Optional wallet provider override. If omitted, the session wallet provider is used."
                        ),
                    },
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
                "Send SBC on Radius Testnet to a recipient address. Uses the selected provider "
                "wallet and returns the tx hash plus explorer URL."
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
                    "provider": {
                        "type": "string",
                        "enum": ["local", "para"],
                        "description": (
                            "Optional wallet provider override. If omitted, the session wallet provider is used."
                        ),
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
