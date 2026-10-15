from copy import deepcopy

from .codec import encode_agent_uri, normalize_registration
from .constants import NetworkConfig


FULL_REGISTRATION_REQUIRED_FIELDS = (
    "type",
    "name",
    "description",
    "image",
    "services",
    "active",
    "supportedTrust",
)


def missing_full_registration_fields(registration: dict) -> list[str]:
    if not isinstance(registration, dict) or not registration:
        return list(FULL_REGISTRATION_REQUIRED_FIELDS)

    missing = []
    for field in FULL_REGISTRATION_REQUIRED_FIELDS:
        if field not in registration:
            missing.append(field)
            continue
        value = registration[field]
        if isinstance(value, str) and not value.strip():
            missing.append(field)
        elif field in {"services", "supportedTrust"} and (
            not isinstance(value, list) or not value
        ):
            missing.append(field)
    return missing


def full_registration_required_error(missing: list[str]) -> str:
    return (
        "erc8004_update_agent_uri requires a complete registration object. "
        "For partial updates, use erc8004_patch_agent_registration or "
        "erc8004_add_ans_pointer. Missing required fields: "
        f"{', '.join(missing)}"
    )


def merge_registration_patch(
    current_registration: dict,
    patch: dict | None,
    *,
    network: NetworkConfig,
) -> tuple[dict, dict]:
    if patch is None:
        patch = {}
    if not isinstance(patch, dict):
        raise ValueError("patch must be a JSON object")

    old_registration = normalize_registration(current_registration, network=network)
    new_registration = deepcopy(old_registration)

    diff = {
        "servicesAdded": [],
        "servicesUpdated": [],
        "aliasesAdded": [],
        "aliasesUpdated": [],
        "externalRegistrationsAdded": [],
        "externalRegistrationsUpdated": [],
        "fieldsChanged": [],
    }

    _apply_fields(new_registration, patch.get("fields"), diff)
    _validate_patch_array(
        patch.get("services_add"),
        "services_add",
        _validate_service_add,
    )
    _validate_patch_array(
        patch.get("services_update"),
        "services_update",
        _validate_service_update,
    )
    _validate_patch_array(patch.get("aliases_add"), "aliases_add", _validate_alias_add)
    _validate_patch_array(
        patch.get("externalRegistrations_add"),
        "externalRegistrations_add",
        _validate_external_registration_add,
    )
    _validate_patch_array(
        patch.get("external_registrations_add"),
        "external_registrations_add",
        _validate_external_registration_add,
    )

    _apply_services(new_registration, patch.get("services_add"), diff, update=False)
    _apply_services(new_registration, patch.get("services_update"), diff, update=True)
    _apply_aliases(new_registration, patch.get("aliases_add"), diff)
    _apply_external_registrations(
        new_registration, patch.get("externalRegistrations_add"), diff
    )
    _apply_external_registrations(
        new_registration, patch.get("external_registrations_add"), diff
    )

    normalized = normalize_registration(new_registration, network=network)
    return normalized, diff


def build_ans_pointer_patch(
    *,
    ans_name: str,
    ans_agent_id: str,
    agent_host: str,
    status: str,
    a2a_url: str,
    web_url: str,
    agent_card_url: str,
    did: str,
) -> dict:
    values = {
        "ans_name": ans_name,
        "ans_agent_id": ans_agent_id,
        "agent_host": agent_host,
        "status": status,
        "a2a_url": a2a_url,
        "web_url": web_url,
        "agent_card_url": agent_card_url,
        "did": did,
    }
    missing = [key for key, value in values.items() if not _non_empty_string(value)]
    if missing:
        raise ValueError(f"Missing required ANS pointer fields: {', '.join(missing)}")

    return {
        "services_add": [
            {
                "name": "web",
                "endpoint": web_url,
                "version": "v1",
                "primary": True,
            },
            {
                "name": "A2A",
                "endpoint": a2a_url,
                "metadata": agent_card_url,
                "version": "0.3.0",
                "primary": True,
            },
            {
                "name": "DID",
                "endpoint": did,
                "version": "v1",
                "primary": True,
            },
            {
                "name": "ANS",
                "endpoint": ans_name,
                "version": "1.0.0",
                "registry": "godaddy-ans",
                "registryId": ans_agent_id,
                "agentHost": agent_host,
                "status": status,
            },
        ],
        "aliases_add": [
            {"type": "web", "endpoint": web_url, "primary": True},
            {"type": "a2a", "endpoint": a2a_url, "primary": True},
            {"type": "did", "endpoint": did, "primary": True},
            {"type": "ans", "endpoint": ans_name, "primary": True},
        ],
        "externalRegistrations_add": [
            {
                "registry": "godaddy-ans",
                "registryId": ans_agent_id,
                "name": ans_name,
                "agentHost": agent_host,
                "version": "1.0.0",
                "status": status,
            }
        ],
    }


def dry_run_patch_result(
    *,
    network_name: str,
    agent_id: int,
    old_registration: dict,
    new_registration: dict,
    diff: dict,
) -> dict:
    data_uri = encode_agent_uri(new_registration)
    return {
        "dryRun": True,
        "network": network_name,
        "agentId": int(agent_id),
        "oldRegistration": old_registration,
        "newRegistration": new_registration,
        "diff": diff,
        "dataUri": data_uri,
        "dataUriBytes": len(data_uri.encode("utf-8")),
        "wouldSubmit": False,
    }


def _apply_fields(registration: dict, fields: dict | None, diff: dict) -> None:
    if fields is None:
        return
    if not isinstance(fields, dict):
        raise ValueError("patch.fields must be an object")
    for key, value in fields.items():
        if registration.get(key) != value:
            registration[key] = deepcopy(value)
            diff["fieldsChanged"].append(key)


def _apply_services(
    registration: dict, services: list[dict] | None, diff: dict, *, update: bool
) -> None:
    if services is None:
        return
    if not isinstance(services, list):
        raise ValueError("patch.services_add and patch.services_update must be arrays")
    target = registration.setdefault("services", [])
    if not isinstance(target, list):
        raise ValueError("registration.services must be an array")
    index = {
        _service_key(item): position
        for position, item in enumerate(target)
        if isinstance(item, dict)
    }
    for service in services:
        if not isinstance(service, dict):
            raise ValueError("patch service entries must be objects")
        key = _service_key(service)
        if key in index:
            position = index[key]
            merged = {**target[position], **deepcopy(service)}
            if merged != target[position]:
                target[position] = merged
                diff["servicesUpdated"].append(merged)
        elif update:
            target.append(deepcopy(service))
            index[key] = len(target) - 1
            diff["servicesAdded"].append(deepcopy(service))
        else:
            target.append(deepcopy(service))
            index[key] = len(target) - 1
            diff["servicesAdded"].append(deepcopy(service))


def _apply_aliases(registration: dict, aliases: list[dict] | None, diff: dict) -> None:
    if aliases is None:
        return
    if not isinstance(aliases, list):
        raise ValueError("patch.aliases_add must be an array")
    target = registration.setdefault("aliases", [])
    if not isinstance(target, list):
        raise ValueError("registration.aliases must be an array")
    index = {
        _alias_key(item): position
        for position, item in enumerate(target)
        if isinstance(item, dict)
    }
    for alias in aliases:
        if not isinstance(alias, dict):
            raise ValueError("patch alias entries must be objects")
        key = _alias_key(alias)
        if key in index:
            position = index[key]
            merged = {**target[position], **deepcopy(alias)}
            if merged != target[position]:
                target[position] = merged
                diff["aliasesUpdated"].append(merged)
        else:
            target.append(deepcopy(alias))
            index[key] = len(target) - 1
            diff["aliasesAdded"].append(deepcopy(alias))


def _apply_external_registrations(
    registration: dict,
    external_registrations: list[dict] | None,
    diff: dict,
) -> None:
    if external_registrations is None:
        return
    if not isinstance(external_registrations, list):
        raise ValueError(
            "patch.externalRegistrations_add and patch.external_registrations_add must be arrays"
        )
    target = registration.setdefault("externalRegistrations", [])
    if not isinstance(target, list):
        raise ValueError("registration.externalRegistrations must be an array")
    index = {
        _external_registration_key(item): position
        for position, item in enumerate(target)
        if isinstance(item, dict)
    }
    for item in external_registrations:
        if not isinstance(item, dict):
            raise ValueError("patch external registration entries must be objects")
        key = _external_registration_key(item)
        if key in index:
            position = index[key]
            merged = {**target[position], **deepcopy(item)}
            if merged != target[position]:
                target[position] = merged
                diff["externalRegistrationsUpdated"].append(merged)
        else:
            target.append(deepcopy(item))
            index[key] = len(target) - 1
            diff["externalRegistrationsAdded"].append(deepcopy(item))


def _service_key(service: dict) -> tuple[str, str]:
    return (
        str(service.get("name", "")).strip().lower(),
        str(service.get("endpoint", "")).strip(),
    )


def _alias_key(alias: dict) -> tuple[str, str]:
    return (
        str(alias.get("type", "")).strip().lower(),
        str(alias.get("endpoint", "")).strip(),
    )


def _external_registration_key(item: dict) -> tuple[str, str]:
    registry = str(item.get("registry", "")).strip().lower()
    registry_id = str(item.get("registryId", "")).strip()
    if registry_id:
        return (registry, registry_id)
    return (
        registry,
        str(item.get("name", "")).strip(),
    )


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_patch_array(items: object, field_name: str, validator) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        raise ValueError(f"patch.{field_name} must be an array")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"patch.{field_name}[{index}] must be an object")
        validator(item, f"patch.{field_name}[{index}]")


def _validate_service_add(item: dict, path: str) -> None:
    if not _non_empty_string(item.get("name")) or not _non_empty_string(
        item.get("endpoint")
    ):
        raise ValueError(f"{path} must include non-empty name and endpoint")


def _validate_service_update(item: dict, path: str) -> None:
    if not _non_empty_string(item.get("name")) or not (
        _non_empty_string(item.get("endpoint")) or item.get("selector")
    ):
        raise ValueError(f"{path} must include non-empty name plus endpoint or selector")


def _validate_alias_add(item: dict, path: str) -> None:
    if not _non_empty_string(item.get("type")) or not _non_empty_string(
        item.get("endpoint")
    ):
        raise ValueError(f"{path} must include non-empty type and endpoint")


def _validate_external_registration_add(item: dict, path: str) -> None:
    if not _non_empty_string(item.get("registry")) or not (
        _non_empty_string(item.get("registryId")) or _non_empty_string(item.get("name"))
    ):
        raise ValueError(
            f"{path} must include non-empty registry and registryId or name"
        )
