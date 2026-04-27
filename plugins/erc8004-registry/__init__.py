import json
import os
import sys
from pathlib import Path

app_root = Path(os.environ.get("HERMES_APP_ROOT", "/app"))
sys.path.insert(0, str(app_root))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from erc8004_registry import (
    DEFAULT_NETWORK,
    MissingSelfRegistrationFields,
    add_ans_pointer,
    get_registration,
    get_registry_stats,
    list_registrations,
    patch_agent_registration,
    register_agent_defaults,
    register_agent,
    self_registration_missing_fields_error,
    supported_networks,
    update_agent_uri,
)


def register(ctx):
    networks = supported_networks()
    service_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "endpoint": {"type": "string"},
            "version": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "domains": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "endpoint"],
    }
    registration_ref_schema = {
        "type": "object",
        "properties": {
            "agentId": {"type": "integer"},
            "agentRegistry": {"type": "string"},
        },
        "required": ["agentId", "agentRegistry"],
    }

    def _as_bool(value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _json_result(fn):
        try:
            return json.dumps(fn(), indent=2)
        except MissingSelfRegistrationFields as err:
            return json.dumps(self_registration_missing_fields_error(err), indent=2)
        except Exception as err:
            return f"Error: {err}"

    def _gas_limit(params, default: int = 2_000_000) -> int:
        value = params.get("gas_limit")
        if value is None:
            value = os.environ.get("ERC8004_GAS_LIMIT")
        return int(value or default)

    def erc8004_get_registration(params, **kwargs):
        params = params or {}
        agent_id = params.get("agent_id")
        if agent_id is None:
            return "Error: missing required parameter 'agent_id'."
        return _json_result(
            lambda: get_registration(
                int(agent_id), params.get("network", DEFAULT_NETWORK)
            )
        )

    def erc8004_list_registrations(params, **kwargs):
        params = params or {}
        return _json_result(
            lambda: list_registrations(
                network=params.get("network", DEFAULT_NETWORK),
                start_id=int(params.get("start_id", 0)),
                limit=int(params.get("limit", 20)),
                include_decoded=_as_bool(params.get("include_decoded"), True),
            )
        )

    def erc8004_get_registry_stats(params, **kwargs):
        params = params or {}
        return _json_result(
            lambda: get_registry_stats(params.get("network", DEFAULT_NETWORK))
        )

    def erc8004_register_agent(params, **kwargs):
        params = params or {}
        registration = params.get("registration")
        if registration is None:
            return (
                "Error: missing required parameter 'registration'. "
                "Use erc8004_register_self for the default self-registration flow, "
                "or provide a full registration object."
            )
        return _json_result(
            lambda: register_agent(
                registration,
                network=params.get("network", DEFAULT_NETWORK),
                gas_limit=_gas_limit(params),
            )
        )

    def erc8004_register_self(params, **kwargs):
        params = params or {}
        return _json_result(
            lambda: register_agent_defaults(
                network=params.get("network", DEFAULT_NETWORK),
                name=(str(params.get("name")).strip() if params.get("name") else None),
                description=(
                    str(params.get("description")).strip()
                    if params.get("description")
                    else None
                ),
                image=(str(params.get("image")).strip() if params.get("image") else None),
                did=(str(params.get("did")).strip() if params.get("did") else None),
                services=params.get("services"),
                x402_support=params.get("x402_support"),
                active=params.get("active"),
                registrations=params.get("registrations"),
                supported_trust=params.get("supported_trust"),
                email=(str(params.get("email")).strip() if params.get("email") else None),
                ens=(str(params.get("ens")).strip() if params.get("ens") else None),
                a2a_version=(
                    str(params.get("a2a_version")).strip()
                    if params.get("a2a_version")
                    else None
                ),
                mcp_endpoint=(
                    str(params.get("mcp_endpoint")).strip()
                    if params.get("mcp_endpoint")
                    else None
                ),
                mcp_version=(
                    str(params.get("mcp_version")).strip()
                    if params.get("mcp_version")
                    else None
                ),
                oasf_endpoint=(
                    str(params.get("oasf_endpoint")).strip()
                    if params.get("oasf_endpoint")
                    else None
                ),
                oasf_version=(
                    str(params.get("oasf_version")).strip()
                    if params.get("oasf_version")
                    else None
                ),
                oasf_skills=params.get("oasf_skills"),
                oasf_domains=params.get("oasf_domains"),
                gas_limit=_gas_limit(params),
            )
        )

    def erc8004_update_agent_uri(params, **kwargs):
        params = params or {}
        agent_id = params.get("agent_id")
        registration = params.get("registration")
        if agent_id is None:
            return "Error: missing required parameter 'agent_id'."
        if registration is None:
            return "Error: missing required parameter 'registration'."
        return _json_result(
            lambda: update_agent_uri(
                int(agent_id),
                registration,
                replace_full_registration=_as_bool(
                    params.get("replace_full_registration"), False
                ),
                network=params.get("network", DEFAULT_NETWORK),
                gas_limit=_gas_limit(params),
            )
        )

    def erc8004_patch_agent_registration(params, **kwargs):
        params = params or {}
        agent_id = params.get("agent_id")
        if agent_id is None:
            return "Error: missing required parameter 'agent_id'."
        return _json_result(
            lambda: patch_agent_registration(
                int(agent_id),
                params.get("patch") or {},
                network=params.get("network", DEFAULT_NETWORK),
                dry_run=_as_bool(params.get("dry_run"), True),
                gas_limit=_gas_limit(params),
            )
        )

    def erc8004_add_ans_pointer(params, **kwargs):
        params = params or {}
        agent_id = params.get("agent_id")
        if agent_id is None:
            return "Error: missing required parameter 'agent_id'."
        return _json_result(
            lambda: add_ans_pointer(
                int(agent_id),
                ans_name=str(params.get("ans_name", "")).strip(),
                ans_agent_id=str(params.get("ans_agent_id", "")).strip(),
                agent_host=str(params.get("agent_host", "")).strip(),
                status=str(params.get("status", "")).strip(),
                a2a_url=str(params.get("a2a_url", "")).strip(),
                web_url=str(params.get("web_url", "")).strip(),
                agent_card_url=str(params.get("agent_card_url", "")).strip(),
                did=str(params.get("did", "")).strip(),
                network=params.get("network", DEFAULT_NETWORK),
                dry_run=_as_bool(params.get("dry_run"), True),
                gas_limit=_gas_limit(params),
            )
        )

    network_property = {
        "type": "string",
        "enum": networks,
        "description": f"Radius network to target. Defaults to {DEFAULT_NETWORK}.",
    }

    ctx.register_tool(
        name="erc8004_get_registration",
        toolset="erc8004-registry",
        schema={
            "name": "erc8004_get_registration",
            "description": "Read a single ERC-8004 registration from the configured Radius registry contract.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network": network_property,
                    "agent_id": {
                        "type": "integer",
                        "description": "ERC-721 token id to inspect.",
                    },
                },
                "required": ["agent_id"],
            },
        },
        handler=erc8004_get_registration,
    )

    ctx.register_tool(
        name="erc8004_list_registrations",
        toolset="erc8004-registry",
        schema={
            "name": "erc8004_list_registrations",
            "description": "List ERC-8004 registrations by iterating token ids from the live registry total supply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network": network_property,
                    "start_id": {
                        "type": "integer",
                        "description": "Starting token id. Defaults to 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of registrations to return. Defaults to 20, max 100.",
                    },
                    "include_decoded": {
                        "type": "boolean",
                        "description": "Whether to decode each data URI into JSON. Defaults to true.",
                    },
                },
                "required": [],
            },
        },
        handler=erc8004_list_registrations,
    )

    ctx.register_tool(
        name="erc8004_get_registry_stats",
        toolset="erc8004-registry",
        schema={
            "name": "erc8004_get_registry_stats",
            "description": "Return the configured ERC-8004 registry address, chain metadata, and total supply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network": network_property,
                },
                "required": [],
            },
        },
        handler=erc8004_get_registry_stats,
    )

    ctx.register_tool(
        name="erc8004_register_agent",
        toolset="erc8004-registry",
        schema={
            "name": "erc8004_register_agent",
            "description": "Normalize, encode, and register an ERC-8004 agent using the configured Radius wallet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network": network_property,
                    "registration": {
                        "type": "object",
                        "description": "Registration JSON object to normalize and encode as a data URI.",
                    },
                    "gas_limit": {
                        "type": "integer",
                        "description": "Optional transaction gas limit. Defaults to 2000000.",
                    },
                },
                "required": ["registration"],
            },
        },
        handler=erc8004_register_agent,
    )

    ctx.register_tool(
        name="erc8004_register_self",
        toolset="erc8004-registry",
        schema={
            "name": "erc8004_register_self",
            "description": (
                "Build a default ERC-8004 registration for the current Hermes agent using "
                "the current runtime URLs plus supplied operator metadata, then submit it. "
                "If required metadata is missing, ask follow-up questions or configure the "
                "matching AGENT_* env vars before calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "network": network_property,
                    "name": {
                        "type": "string",
                        "description": "Agent name. If omitted, AGENT_NAME must be set.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Natural-language agent description. If omitted, AGENT_DESCRIPTION must be set.",
                    },
                    "image": {
                        "type": "string",
                        "description": "Public image URL for the agent. If omitted, AGENT_IMAGE must be set.",
                    },
                    "did": {
                        "type": "string",
                        "description": "Optional DID override for the derived DID service entry.",
                    },
                    "services": {
                        "type": "array",
                        "items": service_schema,
                        "description": "Optional full services array override. If omitted, web/A2A/DID are derived automatically and optional MCP/OASF/ENS/email entries can be added with the fields below.",
                    },
                    "x402_support": {
                        "type": "boolean",
                        "description": "Whether the agent supports x402 payments. Defaults to AGENT_X402_SUPPORT or false.",
                    },
                    "active": {
                        "type": "boolean",
                        "description": "Whether the agent should be advertised as active. Defaults to AGENT_ACTIVE or true.",
                    },
                    "registrations": {
                        "type": "array",
                        "items": registration_ref_schema,
                        "description": "Optional cross-registry registration references with agentId and agentRegistry.",
                    },
                    "supported_trust": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Supported trust modes. If omitted, AGENT_SUPPORTED_TRUST must be set.",
                    },
                    "email": {
                        "type": "string",
                        "description": "Optional email service endpoint.",
                    },
                    "ens": {
                        "type": "string",
                        "description": "Optional ENS name to advertise as an ENS service endpoint.",
                    },
                    "a2a_version": {
                        "type": "string",
                        "description": "Optional A2A version for the derived A2A service. Defaults to AGENT_A2A_VERSION or 0.3.0.",
                    },
                    "mcp_endpoint": {
                        "type": "string",
                        "description": "Optional MCP endpoint.",
                    },
                    "mcp_version": {
                        "type": "string",
                        "description": "Optional MCP version.",
                    },
                    "oasf_endpoint": {
                        "type": "string",
                        "description": "Optional OASF endpoint.",
                    },
                    "oasf_version": {
                        "type": "string",
                        "description": "Optional OASF version.",
                    },
                    "oasf_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional OASF skills list.",
                    },
                    "oasf_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional OASF domains list.",
                    },
                    "gas_limit": {
                        "type": "integer",
                        "description": "Optional transaction gas limit. Defaults to 2000000.",
                    },
                },
                "required": [],
            },
        },
        handler=erc8004_register_self,
    )

    ctx.register_tool(
        name="erc8004_update_agent_uri",
        toolset="erc8004-registry",
        schema={
            "name": "erc8004_update_agent_uri",
            "description": "Full replacement write for an existing ERC-8004 agent URI. Requires a complete registration object; use erc8004_patch_agent_registration or erc8004_add_ans_pointer for partial metadata updates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network": network_property,
                    "agent_id": {
                        "type": "integer",
                        "description": "ERC-721 token id to update.",
                    },
                    "registration": {
                        "type": "object",
                        "description": "Complete registration JSON object to normalize and encode as a data URI.",
                    },
                    "replace_full_registration": {
                        "type": "boolean",
                        "description": "Optional explicit acknowledgement that this is a full replacement operation.",
                    },
                    "gas_limit": {
                        "type": "integer",
                        "description": "Optional transaction gas limit. Defaults to 2000000.",
                    },
                },
                "required": ["agent_id", "registration"],
            },
        },
        handler=erc8004_update_agent_uri,
    )

    ctx.register_tool(
        name="erc8004_patch_agent_registration",
        toolset="erc8004-registry",
        schema={
            "name": "erc8004_patch_agent_registration",
            "description": "Fetch, merge, validate, and optionally submit a partial ERC-8004 registration metadata update. Defaults to dry_run=true and preserves existing services unless explicitly changed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network": network_property,
                    "agent_id": {
                        "type": "integer",
                        "description": "ERC-721 token id to patch.",
                    },
                    "patch": {
                        "type": "object",
                        "description": "Patch object with services_add, services_update, aliases_add, externalRegistrations_add or external_registrations_add, and fields.",
                        "properties": {
                            "services_add": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                            "services_update": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                            "aliases_add": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                            "externalRegistrations_add": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                            "external_registrations_add": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                            "fields": {"type": "object"},
                        },
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "When true, return old/new metadata, diff, and encoded data URI without submitting a transaction. Defaults to true.",
                    },
                    "gas_limit": {
                        "type": "integer",
                        "description": "Optional transaction gas limit for dry_run=false. Defaults to 2000000.",
                    },
                },
                "required": ["agent_id"],
            },
        },
        handler=erc8004_patch_agent_registration,
    )

    ctx.register_tool(
        name="erc8004_add_ans_pointer",
        toolset="erc8004-registry",
        schema={
            "name": "erc8004_add_ans_pointer",
            "description": "Purpose-built helper that adds canonical web/A2A/DID aliases plus a GoDaddy ANS pointer to an existing ERC-8004 registration. Defaults to dry_run=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network": network_property,
                    "agent_id": {
                        "type": "integer",
                        "description": "ERC-721 token id to patch.",
                    },
                    "ans_name": {"type": "string"},
                    "ans_agent_id": {"type": "string"},
                    "agent_host": {"type": "string"},
                    "status": {"type": "string"},
                    "a2a_url": {"type": "string"},
                    "web_url": {"type": "string"},
                    "agent_card_url": {"type": "string"},
                    "did": {"type": "string"},
                    "dry_run": {
                        "type": "boolean",
                        "description": "When true, return old/new metadata, diff, and encoded data URI without submitting a transaction. Defaults to true.",
                    },
                    "gas_limit": {
                        "type": "integer",
                        "description": "Optional transaction gas limit for dry_run=false. Defaults to 2000000.",
                    },
                },
                "required": [
                    "agent_id",
                    "ans_name",
                    "ans_agent_id",
                    "agent_host",
                    "status",
                    "a2a_url",
                    "web_url",
                    "agent_card_url",
                    "did",
                ],
            },
        },
        handler=erc8004_add_ans_pointer,
    )
