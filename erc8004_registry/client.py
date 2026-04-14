import json
import os
import shutil
import subprocess
from pathlib import Path

from .codec import (
    decode_agent_uri,
    encode_agent_uri,
    normalize_registration,
    sanitize_agent_uri,
)
from .constants import NetworkConfig, get_network_config
from .self_registration import build_self_registration


DEFAULT_REGISTER_GAS = 2_000_000
DEFAULT_UPDATE_GAS = 2_000_000


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _radius_dir() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    return Path(hermes_home) / ".radius"


def _private_key_file() -> Path:
    return _radius_dir() / "key"


def _address_file() -> Path:
    return _radius_dir() / "address"


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


def _run_command(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, env=_cast_env())
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        message = stderr or stdout or f"Command failed with exit code {result.returncode}"
        raise RuntimeError(message)
    return (result.stdout or "").strip()


def _cast_bin() -> str:
    configured = os.environ.get("RADIUS_CAST_BIN", "").strip()
    if configured:
        return configured
    discovered = shutil.which("cast")
    if discovered:
        return discovered
    for candidate in (
        "/root/.foundry/bin/cast",
        "/usr/local/bin/cast",
        "/opt/foundry/bin/cast",
        str(_repo_root() / ".foundry" / "bin" / "cast"),
    ):
        if Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "Foundry cast is not installed or not on PATH. Set RADIUS_CAST_BIN or install Foundry."
    )


def _run_cast(args):
    return _run_command([_cast_bin(), *args])


def _resolve_wallet_address() -> str:
    address = _read_address_hint()
    if address:
        return address
    return _run_cast(["wallet", "address", "--private-key", _read_private_key()]).strip()


def _parse_int(output: str) -> int:
    value = output.strip()
    if not value:
        raise ValueError("Expected integer output from cast, got empty string")
    first_token = value.split()[0]
    if first_token.startswith("0x"):
        return int(first_token, 16)
    return int(first_token)


def _call(config: NetworkConfig, signature: str, *args: str) -> str:
    return _run_cast(
        [
            "call",
            config.identity_registry,
            signature,
            *args,
            "--rpc-url",
            config.rpc_url,
        ]
    ).strip()


def _receipt(tx_hash: str, config: NetworkConfig) -> dict:
    return json.loads(
        _run_cast(
            [
                "receipt",
                tx_hash,
                "--rpc-url",
                config.rpc_url,
                "--confirmations",
                "1",
                "--json",
            ]
        )
    )


def _send(
    config: NetworkConfig,
    signature: str,
    args: list[str],
    *,
    gas_limit: int,
) -> str:
    return _run_cast(
        [
            "send",
            config.identity_registry,
            signature,
            *args,
            "--rpc-url",
            config.rpc_url,
            "--private-key",
            _read_private_key(),
            "--chain",
            str(config.chain_id),
            "--gas-limit",
            str(gas_limit),
            "--async",
        ]
    ).strip()


def get_registry_stats(network: str | None = None) -> dict:
    config = get_network_config(network)
    total_supply = _parse_int(_call(config, "totalSupply()(uint256)"))
    return {
        "network": config.name,
        "chain_id": config.chain_id,
        "rpc_url": config.rpc_url,
        "explorer_url": config.explorer_url,
        "identity_registry": config.identity_registry_ref,
        "contract_address": config.identity_registry,
        "total_supply": total_supply,
    }


def get_registration(agent_id: int, network: str | None = None) -> dict:
    config = get_network_config(network)
    raw_uri = _call(config, "tokenURI(uint256)(string)", str(agent_id))
    normalized_uri = sanitize_agent_uri(raw_uri)
    decoded = decode_agent_uri(normalized_uri)
    return {
        "network": config.name,
        "chain_id": config.chain_id,
        "identity_registry": config.identity_registry_ref,
        "contract_address": config.identity_registry,
        "agent_id": int(agent_id),
        "token_uri": raw_uri,
        "normalized_token_uri": normalized_uri,
        "registration": decoded,
    }


def list_registrations(
    *,
    network: str | None = None,
    start_id: int = 0,
    limit: int = 20,
    include_decoded: bool = True,
) -> dict:
    config = get_network_config(network)
    total_supply = _parse_int(_call(config, "totalSupply()(uint256)"))
    start = max(0, int(start_id))
    count = max(0, min(int(limit), 100))
    end = min(total_supply, start + count)
    items = []
    for agent_id in range(start, end):
        token_uri = _call(config, "tokenURI(uint256)(string)", str(agent_id))
        normalized_uri = sanitize_agent_uri(token_uri)
        item = {
            "agent_id": agent_id,
            "token_uri": token_uri,
            "normalized_token_uri": normalized_uri,
        }
        if include_decoded:
            item["registration"] = decode_agent_uri(normalized_uri)
        items.append(item)
    return {
        "network": config.name,
        "chain_id": config.chain_id,
        "identity_registry": config.identity_registry_ref,
        "contract_address": config.identity_registry,
        "total_supply": total_supply,
        "start_id": start,
        "limit": count,
        "items": items,
    }


def register_agent(
    registration: dict,
    *,
    network: str | None = None,
    gas_limit: int = DEFAULT_REGISTER_GAS,
) -> dict:
    config = get_network_config(network)
    before_supply = _parse_int(_call(config, "totalSupply()(uint256)"))
    wallet = _resolve_wallet_address()
    normalized = normalize_registration(
        registration,
        network=config,
    )
    agent_uri = encode_agent_uri(normalized)
    tx_hash = _send(
        config,
        "register(string)",
        [agent_uri],
        gas_limit=int(gas_limit or DEFAULT_REGISTER_GAS),
    )
    receipt = _receipt(tx_hash, config)
    after_supply = _parse_int(_call(config, "totalSupply()(uint256)"))
    agent_id = after_supply - 1 if after_supply > before_supply else None
    return {
        "network": config.name,
        "chain_id": config.chain_id,
        "identity_registry": config.identity_registry_ref,
        "contract_address": config.identity_registry,
        "submitted_by": wallet,
        "tx_hash": tx_hash,
        "explorer_url": f"{config.explorer_url}/tx/{tx_hash}",
        "receipt": receipt,
        "agent_id": agent_id,
        "token_uri": agent_uri,
        "registration": normalized,
    }


def register_agent_defaults(
    *,
    network: str | None = None,
    name: str | None = None,
    description: str | None = None,
    image: str | None = None,
    did: str | None = None,
    services: list[dict] | None = None,
    x402_support: bool | None = None,
    active: bool | None = None,
    registrations: list[dict] | None = None,
    supported_trust: list[str] | None = None,
    email: str | None = None,
    ens: str | None = None,
    a2a_version: str | None = None,
    mcp_endpoint: str | None = None,
    mcp_version: str | None = None,
    oasf_endpoint: str | None = None,
    oasf_version: str | None = None,
    oasf_skills: list[str] | None = None,
    oasf_domains: list[str] | None = None,
    gas_limit: int = DEFAULT_REGISTER_GAS,
) -> dict:
    config = get_network_config(network)
    registration = build_self_registration(
        config,
        name=name,
        description=description,
        image=image,
        did=did,
        services=services,
        x402_support=x402_support,
        active=active,
        registrations=registrations,
        supported_trust=supported_trust,
        email=email,
        ens=ens,
        a2a_version=a2a_version,
        mcp_endpoint=mcp_endpoint,
        mcp_version=mcp_version,
        oasf_endpoint=oasf_endpoint,
        oasf_version=oasf_version,
        oasf_skills=oasf_skills,
        oasf_domains=oasf_domains,
    )
    result = register_agent(
        registration,
        network=config.name,
        gas_limit=gas_limit,
    )
    result["used_defaults"] = True
    return result


def update_agent_uri(
    agent_id: int,
    registration: dict,
    *,
    network: str | None = None,
    gas_limit: int = DEFAULT_UPDATE_GAS,
) -> dict:
    config = get_network_config(network)
    wallet = _resolve_wallet_address()
    normalized = normalize_registration(
        registration,
        network=config,
    )
    agent_uri = encode_agent_uri(normalized)
    tx_hash = _send(
        config,
        "setAgentURI(uint256,string)",
        [str(agent_id), agent_uri],
        gas_limit=int(gas_limit or DEFAULT_UPDATE_GAS),
    )
    receipt = _receipt(tx_hash, config)
    return {
        "network": config.name,
        "chain_id": config.chain_id,
        "identity_registry": config.identity_registry_ref,
        "contract_address": config.identity_registry,
        "submitted_by": wallet,
        "tx_hash": tx_hash,
        "explorer_url": f"{config.explorer_url}/tx/{tx_hash}",
        "receipt": receipt,
        "agent_id": int(agent_id),
        "token_uri": agent_uri,
        "registration": normalized,
    }
