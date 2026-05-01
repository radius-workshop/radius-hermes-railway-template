from dataclasses import dataclass
import os


@dataclass(frozen=True)
class NetworkConfig:
    name: str
    chain_id: int
    rpc_url: str
    explorer_url: str
    identity_registry: str
    enabled: bool

    @property
    def identity_registry_ref(self) -> str:
        return f"eip155:{self.chain_id}:{self.identity_registry}"


RADIUS_TESTNET = NetworkConfig(
    name="testnet",
    chain_id=72344,
    rpc_url="https://rpc.testnet.radiustech.xyz",
    explorer_url="https://testnet.radiustech.xyz",
    identity_registry="0x5cd923Ce1244d5498Bf3f9E0F3a374C2567F1A31",
    enabled=True,
)

RADIUS_MAINNET = NetworkConfig(
    name="mainnet",
    chain_id=0,
    rpc_url="",
    explorer_url="",
    identity_registry="",
    enabled=False,
)

NETWORKS = {
    "testnet": RADIUS_TESTNET,
    "mainnet": RADIUS_MAINNET,
}

DEFAULT_NETWORK = "testnet"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _with_env_overrides(config: NetworkConfig) -> NetworkConfig:
    prefix = f"ERC8004_{config.name.upper()}"
    chain_id = int(_env(f"{prefix}_CHAIN_ID") or config.chain_id)
    rpc_url = _env(f"{prefix}_RPC_URL") or config.rpc_url
    explorer_url = _env(f"{prefix}_EXPLORER_URL") or config.explorer_url
    identity_registry = _env(f"{prefix}_REGISTRY") or config.identity_registry
    enabled = config.enabled
    if config.name == "mainnet":
        enabled = bool(rpc_url and explorer_url and identity_registry and chain_id)
    return NetworkConfig(
        name=config.name,
        chain_id=chain_id,
        rpc_url=rpc_url,
        explorer_url=explorer_url,
        identity_registry=identity_registry,
        enabled=enabled,
    )


def get_network_config(network: str | None) -> NetworkConfig:
    key = (network or _env("ERC8004_NETWORK") or DEFAULT_NETWORK).strip().lower()
    if key not in NETWORKS:
        raise ValueError(
            f"Unsupported network '{network}'. Expected one of: {', '.join(NETWORKS)}."
        )
    config = _with_env_overrides(NETWORKS[key])
    if not config.enabled:
        raise ValueError(
            f"Radius {config.name} is not configured. Set ERC8004_{config.name.upper()}_RPC_URL, "
            f"ERC8004_{config.name.upper()}_REGISTRY, ERC8004_{config.name.upper()}_EXPLORER_URL, "
            f"and ERC8004_{config.name.upper()}_CHAIN_ID before using this network."
        )
    return config


def supported_networks() -> list[str]:
    return list(NETWORKS.keys())
