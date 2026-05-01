import os
from copy import deepcopy
from urllib.parse import urlparse

from .codec import build_registration
from .constants import NetworkConfig


class MissingSelfRegistrationFields(ValueError):
    def __init__(self, missing_fields: list[dict]):
        self.missing_fields = missing_fields
        labels = ", ".join(item["field"] for item in missing_fields)
        super().__init__(
            "Missing required self-registration fields: "
            f"{labels}. Provide them as tool params or env vars."
        )


def build_self_registration(
    network: NetworkConfig,
    *,
    name: str | None = None,
    description: str | None = None,
    image: str | None = None,
    did: str | None = None,
    base_url: str | None = None,
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
) -> dict:
    resolved_base_url = _clean_string(base_url) or _default_base_url()
    resolved_did = _clean_string(did) or _derive_did_web(resolved_base_url)

    missing_fields: list[dict] = []
    resolved_name = _resolve_required_string(
        field="name",
        value=name,
        env_var="AGENT_NAME",
        missing_fields=missing_fields,
    )
    resolved_description = _resolve_required_string(
        field="description",
        value=description,
        env_var="AGENT_DESCRIPTION",
        missing_fields=missing_fields,
    )
    resolved_image = _resolve_required_string(
        field="image",
        value=image,
        env_var="AGENT_IMAGE",
        missing_fields=missing_fields,
    )

    resolved_supported_trust = _resolve_required_list(
        field="supportedTrust",
        value=supported_trust,
        env_var="AGENT_SUPPORTED_TRUST",
        missing_fields=missing_fields,
    )
    if missing_fields:
        raise MissingSelfRegistrationFields(missing_fields)

    resolved_x402_support = _resolve_bool(
        value=x402_support,
        env_var="AGENT_X402_SUPPORT",
        default=False,
    )
    resolved_active = _resolve_bool(
        value=active,
        env_var="AGENT_ACTIVE",
        default=True,
    )

    resolved_services = deepcopy(services) if services is not None else _default_services(
        base_url=resolved_base_url,
        did=resolved_did,
        email=_clean_string(email) or _clean_string(os.environ.get("AGENT_EMAIL")),
        ens=_clean_string(ens) or _clean_string(os.environ.get("AGENT_ENS")),
        a2a_version=_clean_string(a2a_version)
        or _clean_string(os.environ.get("AGENT_A2A_VERSION"))
        or "0.3.0",
        mcp_endpoint=_clean_string(mcp_endpoint)
        or _clean_string(os.environ.get("AGENT_MCP_ENDPOINT")),
        mcp_version=_clean_string(mcp_version)
        or _clean_string(os.environ.get("AGENT_MCP_VERSION")),
        oasf_endpoint=_clean_string(oasf_endpoint)
        or _clean_string(os.environ.get("AGENT_OASF_ENDPOINT")),
        oasf_version=_clean_string(oasf_version)
        or _clean_string(os.environ.get("AGENT_OASF_VERSION")),
        oasf_skills=oasf_skills
        if oasf_skills is not None
        else _csv_list(os.environ.get("AGENT_OASF_SKILLS")),
        oasf_domains=oasf_domains
        if oasf_domains is not None
        else _csv_list(os.environ.get("AGENT_OASF_DOMAINS")),
    )

    resolved_registrations = (
        deepcopy(registrations)
        if registrations is not None
        else _default_registrations(network)
    )
    registration = build_registration(
        network,
        name=resolved_name,
        description=resolved_description,
        image=resolved_image,
        services=resolved_services,
        x402_support=resolved_x402_support,
        active=resolved_active,
        registrations=resolved_registrations,
        supported_trust=resolved_supported_trust,
    )
    registration["aliases"] = []
    registration["externalRegistrations"] = []

    wallet = _clean_string(os.environ.get("AGENT_WALLET")) or _clean_string(
        os.environ.get("RADIUS_WALLET_ADDRESS")
    )
    if wallet:
        registration["agentWallet"] = wallet

    _add_ans_metadata(registration)
    return registration


def self_registration_missing_fields_error(err: MissingSelfRegistrationFields) -> dict:
    return {
        "error": str(err),
        "missing_fields": err.missing_fields,
    }


def _default_base_url() -> str:
    public_url = _clean_string(os.environ.get("PUBLIC_URL"))
    if public_url:
        return public_url
    public_domain = _clean_string(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    if public_domain:
        return f"https://{public_domain}"
    port = _clean_string(os.environ.get("PORT")) or "3000"
    return f"http://localhost:{port}"


def _derive_did_web(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = parsed.netloc.replace(":", "%3A")
    path = parsed.path.strip("/")
    if path:
        return f"did:web:{host}:{':'.join(path.split('/'))}"
    return f"did:web:{host}"


def _default_services(
    *,
    base_url: str,
    did: str,
    email: str | None,
    ens: str | None,
    a2a_version: str,
    mcp_endpoint: str | None,
    mcp_version: str | None,
    oasf_endpoint: str | None,
    oasf_version: str | None,
    oasf_skills: list[str],
    oasf_domains: list[str],
) -> list[dict]:
    normalized_base_url = base_url.rstrip("/")
    services = [
        {
            "name": "web",
            "endpoint": f"{normalized_base_url}/",
        },
        {
            "name": "A2A",
            "endpoint": f"{normalized_base_url}/a2a",
            "metadata": f"{normalized_base_url}/.well-known/agent-card.json",
            "version": a2a_version,
        },
        {
            "name": "DID",
            "endpoint": did,
            "version": "v1",
        },
    ]

    if mcp_endpoint:
        entry = {"name": "MCP", "endpoint": mcp_endpoint}
        if mcp_version:
            entry["version"] = mcp_version
        services.append(entry)

    if oasf_endpoint:
        entry = {"name": "OASF", "endpoint": oasf_endpoint}
        if oasf_version:
            entry["version"] = oasf_version
        if oasf_skills:
            entry["skills"] = oasf_skills
        if oasf_domains:
            entry["domains"] = oasf_domains
        services.append(entry)

    if ens:
        services.append({"name": "ENS", "endpoint": ens, "version": "v1"})

    if email:
        services.append({"name": "email", "endpoint": email})

    return services


def _resolve_required_string(
    *,
    field: str,
    value: str | None,
    env_var: str,
    missing_fields: list[dict],
) -> str | None:
    resolved = _clean_string(value) or _clean_string(os.environ.get(env_var))
    if resolved:
        return resolved
    missing_fields.append({"field": field, "param": field, "env_var": env_var})
    return None


def _resolve_required_list(
    *,
    field: str,
    value: list[str] | None,
    env_var: str,
    missing_fields: list[dict],
) -> list[str]:
    if value is not None:
        cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    else:
        cleaned = _csv_list(os.environ.get(env_var))
    if cleaned:
        return cleaned
    missing_fields.append(
        {"field": field, "param": _snake_case(field), "env_var": env_var}
    )
    return []


def _resolve_bool(*, value: bool | None, env_var: str, default: bool) -> bool:
    if value is not None:
        return bool(value)
    raw = _clean_string(os.environ.get(env_var))
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _clean_string(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _default_registrations(network: NetworkConfig) -> list[dict]:
    raw_agent_id = _clean_string(os.environ.get("AGENT_ERC8004_ID"))
    if raw_agent_id is None:
        return []
    try:
        agent_id = int(raw_agent_id)
    except ValueError:
        return []
    registry = _clean_string(os.environ.get("AGENT_ERC8004_REGISTRY"))
    return [
        {
            "agentId": agent_id,
            "agentRegistry": registry or network.identity_registry_ref,
        }
    ]


def _add_ans_metadata(registration: dict) -> None:
    ans_name = _clean_string(os.environ.get("AGENT_ANS_NAME"))
    ans_agent_id = _clean_string(os.environ.get("AGENT_ANS_AGENT_ID"))
    ans_host = _clean_string(os.environ.get("AGENT_ANS_HOST"))
    ans_status = _clean_string(os.environ.get("AGENT_ANS_STATUS"))
    if not (ans_name and ans_agent_id and ans_host):
        return

    registration["services"].append(
        {
            "name": "ANS",
            "endpoint": ans_name,
            "version": "1.0.0",
            "registry": "godaddy-ans",
            "registryId": ans_agent_id,
            "agentHost": ans_host,
            **({"status": ans_status} if ans_status else {}),
        }
    )
    registration["aliases"].append(
        {"type": "ans", "endpoint": ans_name, "primary": True}
    )
    registration["externalRegistrations"].append(
        {
            "registry": "godaddy-ans",
            "registryId": ans_agent_id,
            "name": ans_name,
            "agentHost": ans_host,
            "version": "1.0.0",
            **({"status": ans_status} if ans_status else {}),
        }
    )


def _snake_case(value: str) -> str:
    chars = []
    for index, ch in enumerate(value):
        if ch.isupper() and index > 0:
            chars.append("_")
        chars.append(ch.lower())
    return "".join(chars)
