from dataclasses import dataclass


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


def get_network_config(network: str | None) -> NetworkConfig:
    key = (network or DEFAULT_NETWORK).strip().lower()
    if key not in NETWORKS:
        raise ValueError(
            f"Unsupported network '{network}'. Expected one of: {', '.join(NETWORKS)}."
        )
    config = NETWORKS[key]
    if not config.enabled:
        raise ValueError(
            f"Radius {config.name} is not enabled yet in this build."
        )
    return config


def supported_networks() -> list[str]:
    return list(NETWORKS.keys())
