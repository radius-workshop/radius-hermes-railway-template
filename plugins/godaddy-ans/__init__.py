from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


app_root = Path(os.environ.get("HERMES_APP_ROOT", "/app"))
sys.path.insert(0, str(app_root))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.godaddy import ans


ANS_TOOLS = [
    "godaddy_ans_capabilities",
    "godaddy_ans_prepare_registration",
    "godaddy_ans_register",
    "godaddy_ans_search",
    "godaddy_ans_get_agent",
    "godaddy_ans_resolve",
    "godaddy_ans_revoke",
    "godaddy_ans_verify_acme",
    "godaddy_ans_verify_dns",
    "godaddy_ans_get_identity_certificates",
    "godaddy_ans_submit_identity_csr",
    "godaddy_ans_get_server_certificates",
    "godaddy_ans_submit_server_csr",
    "godaddy_ans_get_csr_status",
    "godaddy_ans_events",
]


REGISTRATION_STATUS = {
    "status": "swagger_aligned",
    "swagger_source": "https://developer.godaddy.com/swagger/swagger_ans.json",
    "affected_tools": ["godaddy_ans_prepare_registration", "godaddy_ans_register"],
    "recommended_flow": (
        "Use godaddy_ans_prepare_registration to inspect the generated Swagger-shaped "
        "payload, then godaddy_ans_register to submit it when agentHost, endpoint URLs, "
        "and domain validation prerequisites are correct."
    ),
    "payload_requirements": [
        "Required fields: agentDisplayName, identityCsrPEM, version, agentHost, endpoints.",
        "agentDisplayName maxLength 64; agentDescription maxLength 150; agentHost maxLength 253.",
        "version must be Semantic Versioning major.minor.patch.",
        "identityCsrPEM is a base64-encoded PEM CSR.",
        "serverCsrPEM is a base64-encoded PEM CSR when BYOC server certificate fields are not used.",
        "serverCertificatePEM and serverCertificateChainPEM are optional base64-encoded server BYOC fields; identity certificates are always RA-issued.",
        "functions must be nested under AgentEndpoint objects, not top-level.",
        "Each endpoint requires agentUrl and protocol; protocol is A2A, MCP, or HTTP-API.",
    ],
    "csr_requirements": {
        "dns_san": "DNS.1 = <agentHost>",
        "uri_san": "URI.1 = ans://v<version>.<agentHost>",
        "generated_by_tool": True,
    },
}


ACME_GUIDANCE = {
    "http_01": [
        "Write keyAuthorization to $HERMES_HOME/acme-challenges/<token> with no trailing newline.",
        "Verify locally with curl http://localhost:8080/.well-known/acme-challenge/<token>.",
        "Ensure public /.well-known/acme-challenge/* paths pass through any CDN/proxy to the origin.",
        "Then call godaddy_ans_verify_acme(agent_id=...).",
    ],
    "dns_01": [
        "Add TXT record _acme-challenge.<agentHost> with the provided token.",
        "Wait for DNS propagation.",
        "Then call godaddy_ans_verify_acme(agent_id=...); verify-acme validates either HTTP-01 or DNS-01.",
    ],
    "verify_dns": [
        "godaddy_ans_verify_dns is the final external-domain DNS records check.",
        "It verifies the required HTTPS, TLSA, _ans, and _ra-badge records, not the ACME TXT challenge.",
    ],
    "blocked_domains": [
        "*.up.railway.app CAA records block GoDaddy CA issuance.",
        "CDNs that do not proxy /.well-known/acme-challenge/* cause HTTP-01 404s.",
    ],
}


_OVERRIDE_ENV = {
    "godaddy_ans_agent_host": "GODADDY_ANS_AGENT_HOST",
    "godaddy_ans_version": "GODADDY_ANS_VERSION",
    "godaddy_ans_display_name": "GODADDY_ANS_DISPLAY_NAME",
    "godaddy_ans_description": "GODADDY_ANS_DESCRIPTION",
    "godaddy_ans_include_a2a": "GODADDY_ANS_INCLUDE_A2A",
    "godaddy_ans_include_mcp": "GODADDY_ANS_INCLUDE_MCP",
    "godaddy_ans_include_http_api": "GODADDY_ANS_INCLUDE_HTTP_API",
    "godaddy_ans_mcp_url": "GODADDY_ANS_MCP_URL",
    "godaddy_ans_http_api_url": "GODADDY_ANS_HTTP_API_URL",
}


def _go_daddy_turn_context(**kwargs) -> str:
    text_parts: list[str] = []
    for key in ("user_message", "message", "prompt", "input"):
        value = kwargs.get(key)
        if isinstance(value, str):
            text_parts.append(value)
    messages = kwargs.get("messages")
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    text_parts.append(content)

    text = " ".join(text_parts).lower()
    if not any(token in text for token in ("godaddy", "go daddy", "ans", "agent name service")):
        return ""

    return (
        "GoDaddy routing reminder: use GoDaddy MCP tools for domain availability and "
        "domain suggestions. For ANS registry actions, call the local godaddy-ans "
        "plugin tools directly. Use godaddy_ans_search for ANS registry searches, "
        "godaddy_ans_capabilities for capability questions, and "
        "godaddy_ans_prepare_registration for offline registration artifacts. Default "
        "ANS API calls to production; use OTE only when the operator explicitly asks "
        "for it or sets GODADDY_ANS_ENV=ote. Registration payloads follow the ANS "
        "Swagger: base64 CSR fields, no top-level functions, endpoint-local functions, "
        "and DNS plus URI SANs in both CSRs. Use godaddy_ans_prepare_registration to "
        "inspect artifacts before godaddy_ans_register. Do not "
        "inspect plugin files, run /app/scripts/godaddy/ans.py, install packages, "
        "read or print GODADDY_API_KEY/GODADDY_API_SECRET, or set secret env vars by "
        "terminal for normal ANS work; the plugin receives configured credentials "
        "from the environment."
    )


def _capabilities_payload() -> dict[str, Any]:
    api_key = os.environ.get("GODADDY_API_KEY", "").strip()
    api_secret = os.environ.get("GODADDY_API_SECRET", "").strip()
    return {
        "godaddy_mcp": {
            "purpose": "GoDaddy-hosted domain and registrar workflows exposed by the remote MCP server.",
            "configured_by": "mcp_servers.godaddy in Hermes config.yaml",
            "default_url": "https://api.godaddy.com/v1/domains/mcp",
            "use_for": [
                "domain availability checks",
                "domain name suggestions",
                "other GoDaddy domain workflows exposed by the remote MCP server",
            ],
        },
        "godaddy_ans": {
            "purpose": "Agent Name Service registry registration, search, lookup, resolution, and verification.",
            "toolset": "godaddy-ans",
            "default_environment": "production",
            "environment_override": "Set GODADDY_ANS_ENV=ote only when OTE is explicitly required.",
            "search_behavior": (
                "godaddy_ans_search query uses ANS server-side agentDisplayName and "
                "agentHost filters, deduplicates results, and broadens empty long-word "
                "queries before returning a summary."
            ),
            "registration_tool_status": REGISTRATION_STATUS,
            "acme_validation_workflow": ACME_GUIDANCE,
            "pending_registration_inspection": [
                "Use godaddy_ans_get_agent(agent_id=...) after PENDING_VALIDATION.",
                "Inspect registrationPending.status, registrationPending.challenges, registrationPending.expiresAt, and registrationPending.nextSteps.",
            ],
            "resolution_behavior": "godaddy_ans_resolve sends POST /v1/agents/resolution with agentHost and version in the JSON body. Version can be a SemVer range, '*' or empty to match latest.",
            "tools": ANS_TOOLS,
            "credential_status": {
                "live_api_credentials_configured": bool(api_key and api_secret),
                "required_for_live_calls": ["GODADDY_API_KEY", "GODADDY_API_SECRET"],
                "offline_tools": ["godaddy_ans_capabilities", "godaddy_ans_prepare_registration"],
            },
        },
        "default_routing": {
            "domain_search_or_suggestions": "Use GoDaddy MCP.",
            "ans_registration_search_lookup_resolution_or_verification": "Use the godaddy-ans plugin tools.",
        },
    }


def _json_result(fn) -> str:
    try:
        return json.dumps(fn(), indent=2)
    except Exception as err:
        return f"Error: {err}"


def _strip(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _first_non_empty(params: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _strip(params.get(key))
        if value:
            return value
    return None


def _string_env_overrides(params: Mapping[str, Any]) -> dict[str, str]:
    overrides = dict(os.environ)
    for param_name, env_key in _OVERRIDE_ENV.items():
        if param_name not in params or params[param_name] is None:
            continue
        value = params[param_name]
        if isinstance(value, bool):
            overrides[env_key] = "true" if value else "false"
        else:
            stripped = _strip(value)
            if stripped is not None:
                overrides[env_key] = stripped
    return overrides


def _state_dir(params: Mapping[str, Any]) -> str | None:
    return _strip(params.get("state_dir"))


def register(ctx):
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", _go_daddy_turn_context)

    def godaddy_ans_capabilities(params, **kwargs):
        return _json_result(_capabilities_payload)

    def godaddy_ans_prepare_registration(params, **kwargs):
        params = params or {}
        env = _string_env_overrides(params)
        return _json_result(
            lambda: ans.build_registration_bundle(env=env, state_dir=_state_dir(params))
        )

    def godaddy_ans_register(params, **kwargs):
        params = params or {}
        env = _string_env_overrides(params)
        return _json_result(lambda: ans.register_agent(env=env, state_dir=_state_dir(params)))

    def godaddy_ans_search(params, **kwargs):
        params = params or {}
        status = params.get("status")
        if isinstance(status, str):
            status = [status]
        limit = params.get("limit")
        offset = params.get("offset")
        return _json_result(
            lambda: ans.search_agents(
                query=_first_non_empty(params, "query", "search_term", "term"),
                agent_display_name=_first_non_empty(params, "agent_display_name"),
                agent_host=_first_non_empty(params, "agent_host"),
                version=_first_non_empty(params, "version"),
                protocol=_first_non_empty(params, "protocol"),
                limit=int(limit) if limit is not None else None,
                offset=int(offset) if offset is not None else None,
                status=status,
            )
        )

    def godaddy_ans_get_agent(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        return _json_result(lambda: ans.get_agent(agent_id))

    def godaddy_ans_resolve(params, **kwargs):
        params = params or {}
        agent_host = _first_non_empty(params, "agent_host")
        version = _strip(params.get("version"))
        if not agent_host:
            return "Error: missing required parameter 'agent_host'."
        return _json_result(lambda: ans.resolve_agent(agent_host, version or ""))

    def godaddy_ans_revoke(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        reason = _first_non_empty(params, "reason")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        if not reason:
            return "Error: missing required parameter 'reason'."
        return _json_result(
            lambda: ans.revoke_agent(
                agent_id,
                reason,
                comments=_first_non_empty(params, "comments"),
            )
        )

    def godaddy_ans_verify_acme(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        return _json_result(lambda: ans.verify_acme(agent_id))

    def godaddy_ans_verify_dns(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        return _json_result(lambda: ans.verify_dns(agent_id))

    def godaddy_ans_get_identity_certificates(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        return _json_result(lambda: ans.get_identity_certificates(agent_id))

    def godaddy_ans_submit_identity_csr(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        csr_pem = _first_non_empty(params, "csr_pem", "csrPEM")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        if not csr_pem:
            return "Error: missing required parameter 'csr_pem'."
        return _json_result(lambda: ans.submit_identity_csr(agent_id, csr_pem))

    def godaddy_ans_get_server_certificates(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        return _json_result(lambda: ans.get_server_certificates(agent_id))

    def godaddy_ans_submit_server_csr(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        csr_pem = _first_non_empty(params, "csr_pem", "csrPEM")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        if not csr_pem:
            return "Error: missing required parameter 'csr_pem'."
        return _json_result(lambda: ans.submit_server_csr(agent_id, csr_pem))

    def godaddy_ans_get_csr_status(params, **kwargs):
        params = params or {}
        agent_id = _first_non_empty(params, "agent_id")
        csr_id = _first_non_empty(params, "csr_id")
        if not agent_id:
            return "Error: missing required parameter 'agent_id'."
        if not csr_id:
            return "Error: missing required parameter 'csr_id'."
        return _json_result(lambda: ans.get_csr_status(agent_id, csr_id))

    def godaddy_ans_events(params, **kwargs):
        params = params or {}
        limit = params.get("limit")
        return _json_result(
            lambda: ans.get_events(
                provider_id=_first_non_empty(params, "provider_id"),
                last_log_id=_first_non_empty(params, "last_log_id"),
                limit=int(limit) if limit is not None else None,
            )
        )

    override_properties = {
        "state_dir": {
            "type": "string",
            "description": "Optional directory to write generated ANS payload and CSR artifacts into.",
        },
        "godaddy_ans_agent_host": {
            "type": "string",
            "description": "Optional agent host override for bundle generation.",
        },
        "godaddy_ans_version": {
            "type": "string",
            "description": "Optional SemVer override for bundle generation.",
        },
        "godaddy_ans_display_name": {
            "type": "string",
            "description": "Optional display name override for bundle generation.",
        },
        "godaddy_ans_description": {
            "type": "string",
            "description": "Optional description override for bundle generation.",
        },
        "godaddy_ans_include_a2a": {
            "type": "boolean",
            "description": "Whether to include the Hermes A2A endpoint in the generated ANS payload.",
        },
        "godaddy_ans_include_mcp": {
            "type": "boolean",
            "description": "Whether to include an MCP endpoint in the generated ANS payload.",
        },
        "godaddy_ans_include_http_api": {
            "type": "boolean",
            "description": "Whether to include an HTTP-API endpoint in the generated ANS payload.",
        },
        "godaddy_ans_mcp_url": {
            "type": "string",
            "description": "Optional MCP endpoint override for the generated ANS payload.",
        },
        "godaddy_ans_http_api_url": {
            "type": "string",
            "description": "Optional HTTP-API endpoint override for the generated ANS payload.",
        },
    }

    tool_specs = [
        (
            "godaddy_ans_capabilities",
            "Describe this agent's GoDaddy capabilities, including the distinction between GoDaddy MCP domain tools and the local GoDaddy ANS registry tools. This is an offline introspection tool and does not require GoDaddy API credentials.",
            {},
            godaddy_ans_capabilities,
        ),
        (
            "godaddy_ans_prepare_registration",
            "Generate Swagger-aligned GoDaddy ANS registration artifacts for this Hermes agent. This writes CSR and payload files under HERMES_HOME and returns the payload plus file paths without calling the ANS API. The payload uses required fields agentDisplayName, identityCsrPEM, version, agentHost, and endpoints; CSR fields are base64-encoded PEM strings as specified; functions are nested under endpoints; both CSRs include DNS and URI SANs in the format ans://v<version>.<agentHost>.",
            override_properties,
            godaddy_ans_prepare_registration,
        ),
        (
            "godaddy_ans_register",
            "Generate the Swagger-aligned GoDaddy ANS registration payload for this agent and submit it to the configured GoDaddy ANS API using GODADDY_API_KEY and GODADDY_API_SECRET. Use godaddy_ans_prepare_registration first when you need to inspect artifacts. External domains may return PENDING_VALIDATION and require HTTP-01 or DNS-01 completion before godaddy_ans_verify_acme.",
            override_properties,
            godaddy_ans_register,
        ),
        (
            "godaddy_ans_search",
            "Search the GoDaddy ANS registry. Use this tool directly for ANS registry search requests instead of terminal commands, Python scripts, curl, or reading environment secrets. The tool reads configured GoDaddy credentials from the runtime environment. Use query for a loose term search such as 'payments'; query automatically searches ANS server-side display-name and host filters and may broaden empty long-word searches. Use the exact filter fields when you know the display name, host, protocol, version, or status.",
            {
                "query": {
                    "type": "string",
                    "description": "Loose search term for requests like 'search ANS for payments'. The tool searches server-side agentDisplayName and agentHost filters, deduplicates results, and only falls back to broader root terms when the exact term returns no rows.",
                },
                "search_term": {"type": "string", "description": "Alias for query."},
                "term": {"type": "string", "description": "Alias for query."},
                "agent_display_name": {
                    "type": "string",
                    "description": "Exact or API-level display name filter when known.",
                },
                "agent_host": {
                    "type": "string",
                    "description": "Exact host filter, for example agent.example.com.",
                },
                "version": {"type": "string", "description": "Flexible target version filter."},
                "protocol": {
                    "type": "string",
                    "enum": ["A2A", "MCP", "HTTP-API"],
                    "description": "Endpoint protocol filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of registry rows requested from GoDaddy, bounded to the ANS max of 100.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset for the GoDaddy registry query.",
                },
                "status": {
                    "type": "array",
                    "description": "Optional lifecycle status filters.",
                    "items": {
                        "type": "string",
                        "enum": ["PENDING_DNS", "ACTIVE", "DEPRECATED", "REVOKED", "ALL"],
                    },
                },
            },
            godaddy_ans_search,
        ),
        (
            "godaddy_ans_get_agent",
            "Fetch one GoDaddy ANS agent registration by agent id. Use this after PENDING_VALIDATION to inspect registrationPending.status, registrationPending.challenges, registrationPending.expiresAt, and registrationPending.nextSteps.",
            {
                "agent_id": {
                    "type": "string",
                    "description": "Agent UUID returned by the GoDaddy ANS API.",
                }
            },
            godaddy_ans_get_agent,
        ),
        (
            "godaddy_ans_resolve",
            "Resolve an ANS host and version to its registered endpoint information using POST /v1/agents/resolution. Version accepts a SemVer range, '*', omitted, or an empty string to match the latest available version.",
            {
                "agent_host": {"type": "string"},
                "version": {"type": "string"},
            },
            godaddy_ans_resolve,
        ),
        (
            "godaddy_ans_revoke",
            "Revoke an active GoDaddy ANS agent or cancel an eligible pending registration. PENDING_VALIDATION registrations cannot be cancelled by this API and expire if ACME verification is not completed.",
            {
                "agent_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "enum": [
                        "KEY_COMPROMISE",
                        "CESSATION_OF_OPERATION",
                        "AFFILIATION_CHANGED",
                        "SUPERSEDED",
                        "CERTIFICATE_HOLD",
                        "PRIVILEGE_WITHDRAWN",
                        "AA_COMPROMISE",
                    ],
                },
                "comments": {"type": "string", "description": "Optional comments, max 200 characters."},
            },
            godaddy_ans_revoke,
        ),
        (
            "godaddy_ans_verify_acme",
            "Trigger GoDaddy ANS ACME domain-control validation for a pending registration. Before calling, complete HTTP-01 by writing keyAuthorization to $HERMES_HOME/acme-challenges/<token> and verifying http://localhost:8080/.well-known/acme-challenge/<token>, or complete DNS-01 by adding the _acme-challenge.<agentHost> TXT record. The RA automatically determines which challenge is discoverable.",
            {"agent_id": {"type": "string"}},
            godaddy_ans_verify_acme,
        ),
        (
            "godaddy_ans_verify_dns",
            "Trigger GoDaddy ANS final DNS records verification for external domain registration. This checks required HTTPS, TLSA, _ans, and _ra-badge records after certificates/DNS provisioning; it is not the ACME TXT challenge verifier.",
            {"agent_id": {"type": "string"}},
            godaddy_ans_verify_dns,
        ),
        (
            "godaddy_ans_get_identity_certificates",
            "Retrieve all identity certificates for a GoDaddy ANS agent.",
            {"agent_id": {"type": "string"}},
            godaddy_ans_get_identity_certificates,
        ),
        (
            "godaddy_ans_submit_identity_csr",
            "Submit a CSR for the agent's identity certificate. The ANS API expects csrPEM as a base64-encoded PEM CSR; this tool accepts raw PEM and encodes it, or accepts an already base64-encoded value.",
            {"agent_id": {"type": "string"}, "csr_pem": {"type": "string"}},
            godaddy_ans_submit_identity_csr,
        ),
        (
            "godaddy_ans_get_server_certificates",
            "Retrieve all TLS server certificates for a GoDaddy ANS agent.",
            {"agent_id": {"type": "string"}},
            godaddy_ans_get_server_certificates,
        ),
        (
            "godaddy_ans_submit_server_csr",
            "Submit a CSR for the agent's server certificate. The ANS API expects csrPEM as a base64-encoded PEM CSR; this tool accepts raw PEM and encodes it, or accepts an already base64-encoded value.",
            {"agent_id": {"type": "string"}, "csr_pem": {"type": "string"}},
            godaddy_ans_submit_server_csr,
        ),
        (
            "godaddy_ans_get_csr_status",
            "Retrieve the current status of an identity or server certificate CSR. Status values are PENDING, SIGNED, or REJECTED; failureReason explains rejections.",
            {"agent_id": {"type": "string"}, "csr_id": {"type": "string"}},
            godaddy_ans_get_csr_status,
        ),
        (
            "godaddy_ans_events",
            "Retrieve a paginated list of ANS agent events. Optional provider_id filters by provider; last_log_id is the pagination cursor; events are retained for 30 days.",
            {
                "provider_id": {"type": "string"},
                "last_log_id": {"type": "string"},
                "limit": {"type": "integer", "description": "Number of events to return, 1-200, default 100."},
            },
            godaddy_ans_events,
        ),
    ]

    for name, description, properties, handler in tool_specs:
        ctx.register_tool(
            name=name,
            toolset="godaddy-ans",
            schema={
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [],
                },
            },
            handler=handler,
        )
