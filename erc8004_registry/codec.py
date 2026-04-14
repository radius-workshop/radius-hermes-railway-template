import base64
import json
from copy import deepcopy

from .constants import NetworkConfig


CANONICAL_FIELD_ORDER = [
    "type",
    "name",
    "description",
    "image",
    "services",
    "x402Support",
    "active",
    "registrations",
    "supportedTrust",
]

REGISTRATION_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"


def build_registration(
    network: NetworkConfig,
    *,
    name: str,
    description: str,
    image: str,
    services: list[dict] | None = None,
    x402_support: bool = False,
    active: bool = True,
    registrations: list[dict] | None = None,
    supported_trust: list[str] | None = None,
) -> dict:
    registration = {
        "type": REGISTRATION_TYPE,
        "name": name,
        "description": description,
        "image": image,
        "services": deepcopy(services or []),
        "x402Support": bool(x402_support),
        "active": bool(active),
        "registrations": deepcopy(registrations or []),
        "supportedTrust": list(supported_trust or []),
    }
    return _order_registration(registration)


def normalize_registration(
    registration: dict,
    *,
    network: NetworkConfig,
    default_name: str | None = None,
    default_description: str | None = None,
    default_image: str | None = None,
    default_services: list[dict] | None = None,
    default_x402_support: bool | None = None,
    default_active: bool | None = None,
    default_registrations: list[dict] | None = None,
    default_supported_trust: list[str] | None = None,
) -> dict:
    if not isinstance(registration, dict):
        raise ValueError("registration must be a JSON object")

    normalized = deepcopy(registration)
    if not normalized.get("type"):
        normalized["type"] = REGISTRATION_TYPE
    elif normalized["type"] != REGISTRATION_TYPE:
        raise ValueError(f"registration.type must be {REGISTRATION_TYPE}")

    if not normalized.get("name"):
        if default_name:
            normalized["name"] = default_name
        else:
            raise ValueError("registration.name is required")

    if not normalized.get("description"):
        if default_description:
            normalized["description"] = default_description
        else:
            raise ValueError("registration.description is required")

    if not normalized.get("image"):
        if default_image:
            normalized["image"] = default_image
        else:
            raise ValueError("registration.image is required")

    services = normalized.get("services")
    if services is None:
        normalized["services"] = deepcopy(default_services or [])
    elif not isinstance(services, list):
        raise ValueError("registration.services must be an array")

    if "x402Support" not in normalized:
        normalized["x402Support"] = (
            bool(default_x402_support) if default_x402_support is not None else False
        )
    else:
        normalized["x402Support"] = bool(normalized["x402Support"])

    if "active" not in normalized:
        normalized["active"] = (
            bool(default_active) if default_active is not None else True
        )
    else:
        normalized["active"] = bool(normalized["active"])

    registrations = normalized.get("registrations")
    if registrations is None:
        normalized["registrations"] = deepcopy(default_registrations or [])
    elif not isinstance(registrations, list):
        raise ValueError("registration.registrations must be an array")

    supported_trust = normalized.get("supportedTrust")
    if supported_trust is None:
        normalized["supportedTrust"] = list(default_supported_trust or [])
    elif not isinstance(supported_trust, list):
        raise ValueError("registration.supportedTrust must be an array")

    _validate_registration(normalized)
    return _order_registration(normalized)


def encode_agent_uri(registration: dict) -> str:
    payload = json.dumps(
        _order_registration(registration),
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:application/json;base64,{encoded}"


def decode_agent_uri(agent_uri: str) -> dict:
    normalized_uri = sanitize_agent_uri(agent_uri)
    if not isinstance(normalized_uri, str) or not normalized_uri.startswith(
        "data:application/json;base64,"
    ):
        raise ValueError(
            "Unsupported agent URI format. Expected data:application/json;base64,... "
            "If the contract returned a quoted tokenURI, strip the wrapping quotes before decoding."
        )
    encoded = normalized_uri.split(",", 1)[1]
    decoded = base64.b64decode(encoded)
    registration = json.loads(decoded.decode("utf-8"))
    if not isinstance(registration, dict):
        raise ValueError("Decoded agent URI is not a JSON object")
    return registration


def sanitize_agent_uri(agent_uri: str) -> str:
    if not isinstance(agent_uri, str):
        raise ValueError("Agent URI must be a string")
    normalized = agent_uri.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == '"':
        normalized = normalized[1:-1]
    return normalized


def _validate_registration(registration: dict) -> None:
    for field in (
        "type",
        "name",
        "description",
        "image",
        "services",
        "x402Support",
        "active",
        "registrations",
        "supportedTrust",
    ):
        if field not in registration:
            raise ValueError(f"registration.{field} is required")

    if registration["type"] != REGISTRATION_TYPE:
        raise ValueError(f"registration.type must be {REGISTRATION_TYPE}")

    if not isinstance(registration["name"], str) or not registration["name"].strip():
        raise ValueError("registration.name must be a non-empty string")

    for field in ("description", "image"):
        if not isinstance(registration[field], str) or not registration[field].strip():
            raise ValueError(f"registration.{field} must be a non-empty string")

    if not isinstance(registration["services"], list) or not registration["services"]:
        raise ValueError("registration.services must be a non-empty array")
    for index, service in enumerate(registration["services"]):
        if not isinstance(service, dict):
            raise ValueError(f"registration.services[{index}] must be an object")
        for field in ("name", "endpoint"):
            value = service.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"registration.services[{index}].{field} must be a non-empty string"
                )
        version = service.get("version")
        if version is not None and (
            not isinstance(version, str) or not version.strip()
        ):
            raise ValueError(
                f"registration.services[{index}].version must be a non-empty string"
            )
        for list_field in ("skills", "domains"):
            if list_field in service and (
                not isinstance(service[list_field], list)
                or not all(
                    isinstance(item, str) and item.strip()
                    for item in service[list_field]
                )
            ):
                raise ValueError(
                    f"registration.services[{index}].{list_field} must be a string array"
                )

    if not isinstance(registration["registrations"], list):
        raise ValueError("registration.registrations must be an array")
    for index, item in enumerate(registration["registrations"]):
        if not isinstance(item, dict):
            raise ValueError(f"registration.registrations[{index}] must be an object")
        if not isinstance(item.get("agentId"), int):
            raise ValueError(
                f"registration.registrations[{index}].agentId must be an integer"
            )
        agent_registry = item.get("agentRegistry")
        if not isinstance(agent_registry, str) or not agent_registry.strip():
            raise ValueError(
                f"registration.registrations[{index}].agentRegistry must be a non-empty string"
            )

    supported_trust = registration.get("supportedTrust")
    if not isinstance(supported_trust, list) or not supported_trust or not all(
        isinstance(item, str) and item.strip() for item in supported_trust
    ):
        raise ValueError("registration.supportedTrust must be a non-empty string array")


def _order_registration(registration: dict) -> dict:
    ordered = {}
    for key in CANONICAL_FIELD_ORDER:
        if key in registration:
            ordered[key] = registration[key]
    for key in sorted(registration.keys()):
        if key not in ordered:
            ordered[key] = registration[key]
    return ordered
