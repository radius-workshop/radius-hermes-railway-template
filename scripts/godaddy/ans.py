"""GoDaddy ANS helper utilities for this Hermes template."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


DEFAULT_ANS_PRODUCTION_BASE_URL = "https://api.godaddy.com"
DEFAULT_ANS_OTE_BASE_URL = "https://api.ote-godaddy.com"
DEFAULT_ANS_STATE_DIR = "godaddy/ans"
DEFAULT_MCP_TRANSPORT = "STREAMABLE-HTTP"
MAX_AGENT_DISPLAY_NAME_LENGTH = 64
MAX_AGENT_DESCRIPTION_LENGTH = 150
MAX_AGENT_HOST_LENGTH = 253
MAX_FUNCTION_ID_LENGTH = 64
MAX_FUNCTION_NAME_LENGTH = 64
MAX_SEARCH_LIMIT = 100
MAX_EVENT_LIMIT = 200
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)$"
)
VALID_PROTOCOLS = {"A2A", "MCP", "HTTP-API"}
VALID_TRANSPORTS = {"STREAMABLE-HTTP", "SSE", "JSON-RPC", "GRPC", "REST", "HTTP"}
VALID_AGENT_STATUSES = {"PENDING_DNS", "ACTIVE", "DEPRECATED", "REVOKED", "ALL"}
VALID_DNS_RECORD_TYPES = {"A", "AAAA", "CNAME", "MX", "NS", "SOA", "SRV", "TXT"}
VALID_REVOCATION_REASONS = {
    "KEY_COMPROMISE",
    "CESSATION_OF_OPERATION",
    "AFFILIATION_CHANGED",
    "SUPERSEDED",
    "CERTIFICATE_HOLD",
    "PRIVILEGE_WITHDRAWN",
    "AA_COMPROMISE",
}
LIVE_API_CREDENTIAL_KEYS = ("GODADDY_API_KEY", "GODADDY_API_SECRET")


def _env(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return env or os.environ


def _is_true(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "on", "yes", "true"}


def _hermes_home(env: Mapping[str, str] | None = None) -> Path:
    current_env = _env(env)
    return Path(current_env.get("HERMES_HOME", "/data/.hermes")).expanduser().resolve()


def _state_dir(
    env: Mapping[str, str] | None = None,
    state_dir: str | Path | None = None,
) -> Path:
    if state_dir:
        return Path(state_dir).expanduser().resolve()
    return (_hermes_home(env) / DEFAULT_ANS_STATE_DIR).resolve()


def _normalized_base_url(env: Mapping[str, str] | None = None) -> str:
    current_env = _env(env)
    public_url = current_env.get("PUBLIC_URL", "").strip()
    if public_url:
        return public_url.rstrip("/")

    public_domain = current_env.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if public_domain:
        return f"https://{public_domain}".rstrip("/")

    port = current_env.get("PORT", "3000").strip() or "3000"
    return f"http://localhost:{port}"


def _normalize_agent_host(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    host = parsed.hostname if parsed.scheme and parsed.hostname else candidate
    host = host.strip().strip(".").lower()
    if not host or "/" in host or ":" in host:
        raise ValueError("agentHost must be a hostname, not a URL or host:port value.")
    if len(host) > MAX_AGENT_HOST_LENGTH:
        raise ValueError(f"agentHost must be {MAX_AGENT_HOST_LENGTH} characters or fewer.")
    return host


def _agent_base_url(
    env: Mapping[str, str] | None = None,
    agent_host: str | None = None,
) -> str:
    current_env = _env(env)
    host = agent_host or _agent_host(current_env)
    base_url = _normalized_base_url(current_env)
    parsed = urlparse(base_url)
    if parsed.hostname == host:
        return base_url
    return f"https://{host}"


def _agent_host(env: Mapping[str, str] | None = None) -> str:
    current_env = _env(env)
    explicit = current_env.get("GODADDY_ANS_AGENT_HOST", "").strip()
    if explicit:
        return _normalize_agent_host(explicit)

    parsed = urlparse(_normalized_base_url(current_env))
    if parsed.hostname:
        return _normalize_agent_host(parsed.hostname)

    raise ValueError(
        "Could not derive agent host. Set GODADDY_ANS_AGENT_HOST or PUBLIC_URL."
    )


def _ans_base_url(env: Mapping[str, str] | None = None) -> str:
    current_env = _env(env)
    explicit = current_env.get("GODADDY_ANS_API_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    env_name = current_env.get("GODADDY_ANS_ENV", "production").strip().lower()
    if env_name in {"", "prod", "production"}:
        return DEFAULT_ANS_PRODUCTION_BASE_URL
    return DEFAULT_ANS_OTE_BASE_URL


def _auth_header(env: Mapping[str, str] | None = None) -> str:
    current_env = _env(env)
    api_key = current_env.get("GODADDY_API_KEY", "").strip()
    api_secret = current_env.get("GODADDY_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise ValueError(
            "GODADDY_API_KEY and GODADDY_API_SECRET are required for GoDaddy ANS API calls."
        )
    return f"sso-key {api_key}:{api_secret}"


def credential_status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    current_env = _env(env)
    missing = [key for key in LIVE_API_CREDENTIAL_KEYS if not current_env.get(key, "").strip()]
    return {
        "live_api_credentials_configured": not missing,
        "missing": missing,
        "required_for_live_calls": list(LIVE_API_CREDENTIAL_KEYS),
        "offline_tools": ["godaddy_ans_capabilities", "godaddy_ans_prepare_registration"],
    }


def _mcp_endpoint(env: Mapping[str, str] | None = None) -> str | None:
    current_env = _env(env)
    explicit = current_env.get("GODADDY_ANS_MCP_URL", "").strip()
    if explicit:
        return explicit
    derived = current_env.get("AGENT_MCP_ENDPOINT", "").strip()
    return derived or None


def _normalized_transport(value: str) -> str:
    transport = value.strip().upper()
    if transport not in VALID_TRANSPORTS:
        raise ValueError(
            "transport must be one of STREAMABLE-HTTP, SSE, JSON-RPC, GRPC, REST, or HTTP."
        )
    return transport


def _public_skill_functions(env: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    skills_root = _hermes_home(env) / "well-known-skills"
    if not skills_root.exists():
        return []

    functions: list[dict[str, str]] = []
    for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        skill_name = skill_dir.name
        functions.append(
            {
                "id": skill_name[:MAX_FUNCTION_ID_LENGTH],
                "name": skill_name.replace("-", " ").title()[:MAX_FUNCTION_NAME_LENGTH],
            }
        )
    return functions


def _build_endpoints(
    env: Mapping[str, str] | None = None,
    *,
    agent_host: str | None = None,
    functions: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    current_env = _env(env)
    base_url = _agent_base_url(current_env, agent_host)
    endpoints: list[dict[str, Any]] = []

    if _is_true(current_env.get("GODADDY_ANS_INCLUDE_A2A"), True):
        a2a_endpoint = {
            "protocol": "A2A",
            "agentUrl": current_env.get("GODADDY_ANS_A2A_URL", "").strip()
            or f"{base_url}/a2a",
            "metaDataUrl": current_env.get("GODADDY_ANS_A2A_METADATA_URL", "").strip()
            or f"{base_url}/.well-known/agent-card.json",
            "transports": ["JSON-RPC"],
        }
        if functions:
            a2a_endpoint["functions"] = functions
        endpoints.append(a2a_endpoint)

    if _is_true(current_env.get("GODADDY_ANS_INCLUDE_MCP"), False):
        mcp_url = _mcp_endpoint(current_env)
        if mcp_url:
            mcp_endpoint = {
                "protocol": "MCP",
                "agentUrl": mcp_url,
                "transports": [
                    _normalized_transport(
                        current_env.get(
                            "GODADDY_ANS_MCP_TRANSPORT", DEFAULT_MCP_TRANSPORT
                        )
                        or DEFAULT_MCP_TRANSPORT
                    )
                ],
            }
            metadata_url = current_env.get("GODADDY_ANS_MCP_METADATA_URL", "").strip()
            documentation_url = current_env.get(
                "GODADDY_ANS_MCP_DOCUMENTATION_URL", ""
            ).strip()
            if metadata_url:
                mcp_endpoint["metaDataUrl"] = metadata_url
            if documentation_url:
                mcp_endpoint["documentationUrl"] = documentation_url
            endpoints.append(mcp_endpoint)

    if _is_true(current_env.get("GODADDY_ANS_INCLUDE_HTTP_API"), False):
        http_api_url = current_env.get("GODADDY_ANS_HTTP_API_URL", "").strip() or base_url
        http_api_endpoint = {
            "protocol": "HTTP-API",
            "agentUrl": http_api_url,
            "transports": ["REST"],
        }
        docs_url = current_env.get("GODADDY_ANS_HTTP_API_DOCS_URL", "").strip()
        if docs_url:
            http_api_endpoint["documentationUrl"] = docs_url
        endpoints.append(http_api_endpoint)

    if not endpoints:
        raise ValueError(
            "No ANS endpoints were enabled. Enable at least one of "
            "GODADDY_ANS_INCLUDE_A2A, GODADDY_ANS_INCLUDE_MCP, or "
            "GODADDY_ANS_INCLUDE_HTTP_API."
        )

    return endpoints


def _subject(agent_host: str, display_name: str):
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    attributes = [
        x509.NameAttribute(NameOID.COMMON_NAME, agent_host),
    ]
    if display_name:
        attributes.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, display_name[:64]))
    return x509.Name(attributes)


def _ans_name(agent_host: str, version: str) -> str:
    normalized_version = str(version).strip()
    if not normalized_version.startswith("v"):
        normalized_version = f"v{normalized_version}"
    return f"ans://{normalized_version}.{agent_host}"


def _registration_version(env: Mapping[str, str] | None = None) -> str:
    current_env = _env(env)
    raw_version = current_env.get("GODADDY_ANS_VERSION", "1.0.0").strip() or "1.0.0"
    version = raw_version[1:] if raw_version.startswith("v") else raw_version
    if not SEMVER_RE.match(version):
        raise ValueError("GODADDY_ANS_VERSION must be a Semantic Versioning value such as 1.0.0.")
    return version


def _load_or_create_key(key_path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if key_path.exists():
        return serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    return key


def _load_or_create_csr(
    key,
    csr_path: Path,
    agent_host: str,
    display_name: str,
    version: str,
):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    required_uri = _ans_name(agent_host, version)
    if csr_path.exists():
        csr = x509.load_pem_x509_csr(csr_path.read_bytes())
        try:
            san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            dns_names = san.get_values_for_type(x509.DNSName)
            uri_names = san.get_values_for_type(x509.UniformResourceIdentifier)
            if agent_host in dns_names and required_uri in uri_names:
                return csr
        except x509.ExtensionNotFound:
            # Existing CSRs without SANs are stale for ANS registration; regenerate below.
            csr = None

    builder = x509.CertificateSigningRequestBuilder().subject_name(
        _subject(agent_host, display_name)
    )
    builder = builder.add_extension(
        x509.SubjectAlternativeName(
            [
                x509.DNSName(agent_host),
                x509.UniformResourceIdentifier(required_uri),
            ]
        ),
        critical=False,
    )
    csr = builder.sign(key, hashes.SHA256())
    csr_path.parent.mkdir(parents=True, exist_ok=True)
    csr_path.write_bytes(csr.public_bytes(serialization.Encoding.PEM))
    return csr


def _b64_pem(pem_bytes: bytes) -> str:
    return base64.b64encode(pem_bytes).decode("ascii")


def _validation_issue(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message}


def _is_base64_pem_csr(value: Any, *, agent_host: str, version: str) -> tuple[bool, str | None]:
    from cryptography import x509

    if not isinstance(value, str) or not value.strip():
        return False, "must be a non-empty base64-encoded PEM CSR string"
    try:
        pem = base64.b64decode(value, validate=True)
    except Exception:
        return False, "must be valid base64"
    if not pem.startswith(b"-----BEGIN CERTIFICATE REQUEST-----"):
        return False, "decoded value must be a PEM certificate request"
    try:
        csr = x509.load_pem_x509_csr(pem)
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception as err:
        return False, f"decoded CSR could not be parsed: {err}"

    required_uri = _ans_name(agent_host, version)
    dns_names = san.get_values_for_type(x509.DNSName)
    uri_names = san.get_values_for_type(x509.UniformResourceIdentifier)
    if agent_host not in dns_names:
        return False, f"CSR SANs must include DNS:{agent_host}"
    if required_uri not in uri_names:
        return False, f"CSR SANs must include URI:{required_uri}"
    return True, None


def validate_registration_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []

    for field in ("agentDisplayName", "identityCsrPEM", "version", "agentHost", "endpoints"):
        if field not in payload:
            issues.append(_validation_issue(field, "is required"))

    display_name = payload.get("agentDisplayName")
    if isinstance(display_name, str):
        if not display_name.strip():
            issues.append(_validation_issue("agentDisplayName", "must not be blank"))
        if len(display_name) > MAX_AGENT_DISPLAY_NAME_LENGTH:
            issues.append(
                _validation_issue(
                    "agentDisplayName",
                    f"must be {MAX_AGENT_DISPLAY_NAME_LENGTH} characters or fewer",
                )
            )
    elif "agentDisplayName" in payload:
        issues.append(_validation_issue("agentDisplayName", "must be a string"))

    description = payload.get("agentDescription")
    if description is not None:
        if not isinstance(description, str):
            issues.append(_validation_issue("agentDescription", "must be a string"))
        elif len(description) > MAX_AGENT_DESCRIPTION_LENGTH:
            issues.append(
                _validation_issue(
                    "agentDescription",
                    f"must be {MAX_AGENT_DESCRIPTION_LENGTH} characters or fewer",
                )
            )

    version = payload.get("version")
    if isinstance(version, str):
        if not SEMVER_RE.match(version):
            issues.append(
                _validation_issue(
                    "version",
                    "must be Semantic Versioning major.minor.patch without prerelease/build suffixes",
                )
            )
    elif "version" in payload:
        issues.append(_validation_issue("version", "must be a string"))

    agent_host = payload.get("agentHost")
    normalized_host: str | None = None
    if isinstance(agent_host, str):
        try:
            normalized_host = _normalize_agent_host(agent_host)
            if normalized_host != agent_host:
                issues.append(_validation_issue("agentHost", "must be a normalized hostname"))
        except ValueError as err:
            issues.append(_validation_issue("agentHost", str(err)))
    elif "agentHost" in payload:
        issues.append(_validation_issue("agentHost", "must be a string"))

    if "functions" in payload:
        issues.append(_validation_issue("functions", "must be nested inside endpoint objects"))

    has_server_csr = bool(payload.get("serverCsrPEM"))
    has_server_cert = bool(payload.get("serverCertificatePEM"))
    has_server_chain = bool(payload.get("serverCertificateChainPEM"))
    if not has_server_csr and not has_server_cert:
        issues.append(
            _validation_issue(
                "serverCsrPEM",
                "is required when serverCertificatePEM is not supplied",
            )
        )
    if has_server_chain and not has_server_cert:
        issues.append(
            _validation_issue(
                "serverCertificateChainPEM",
                "requires serverCertificatePEM",
            )
        )

    if normalized_host and isinstance(version, str) and SEMVER_RE.match(version):
        for field in ("identityCsrPEM", "serverCsrPEM"):
            if field in payload and payload.get(field):
                valid, message = _is_base64_pem_csr(
                    payload[field],
                    agent_host=normalized_host,
                    version=version,
                )
                if not valid and message:
                    issues.append(_validation_issue(field, message))

    endpoints = payload.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        if "endpoints" in payload:
            issues.append(_validation_issue("endpoints", "must be a non-empty array"))
    else:
        for index, endpoint in enumerate(endpoints):
            path = f"endpoints[{index}]"
            if not isinstance(endpoint, Mapping):
                issues.append(_validation_issue(path, "must be an object"))
                continue
            agent_url = endpoint.get("agentUrl")
            if not isinstance(agent_url, str) or not agent_url.strip():
                issues.append(_validation_issue(f"{path}.agentUrl", "is required"))
            protocol = endpoint.get("protocol")
            if not isinstance(protocol, str) or protocol not in VALID_PROTOCOLS:
                issues.append(
                    _validation_issue(
                        f"{path}.protocol",
                        "must be one of A2A, MCP, or HTTP-API",
                    )
                )
            transports = endpoint.get("transports")
            if transports is not None:
                if not isinstance(transports, list) or not all(
                    isinstance(item, str) and item in VALID_TRANSPORTS
                    for item in transports
                ):
                    issues.append(
                        _validation_issue(
                            f"{path}.transports",
                            "must contain only valid transport strings",
                        )
                    )
            functions = endpoint.get("functions")
            if functions is not None:
                if not isinstance(functions, list):
                    issues.append(_validation_issue(f"{path}.functions", "must be an array"))
                else:
                    for function_index, function in enumerate(functions):
                        function_path = f"{path}.functions[{function_index}]"
                        if not isinstance(function, Mapping):
                            issues.append(_validation_issue(function_path, "must be an object"))
                            continue
                        for field in ("id", "name"):
                            value = function.get(field)
                            if not isinstance(value, str) or not value.strip():
                                issues.append(_validation_issue(f"{function_path}.{field}", "is required"))
                            elif len(value) > MAX_FUNCTION_ID_LENGTH:
                                issues.append(
                                    _validation_issue(
                                        f"{function_path}.{field}",
                                        f"must be {MAX_FUNCTION_ID_LENGTH} characters or fewer",
                                    )
                                )

    return {"valid": not issues, "issues": issues}


def build_registration_bundle(
    env: Mapping[str, str] | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization

    current_env = _env(env)
    out_dir = _state_dir(current_env, state_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    agent_host = _agent_host(current_env)
    display_name = (
        current_env.get("GODADDY_ANS_DISPLAY_NAME", "").strip()
        or current_env.get("AGENT_NAME", "").strip()
        or "Hermes Agent"
    )[:MAX_AGENT_DISPLAY_NAME_LENGTH]
    version = _registration_version(current_env)
    description = (
        current_env.get("GODADDY_ANS_DESCRIPTION", "").strip()
        or current_env.get("AGENT_DESCRIPTION", "").strip()
    )
    if description:
        description = description[:MAX_AGENT_DESCRIPTION_LENGTH]

    identity_key_path = out_dir / "identity.key.pem"
    identity_csr_path = out_dir / "identity.csr.pem"
    server_key_path = out_dir / "server.key.pem"
    server_csr_path = out_dir / "server.csr.pem"
    payload_path = out_dir / "registration-payload.json"
    summary_path = out_dir / "bootstrap-summary.json"

    identity_key = _load_or_create_key(identity_key_path)
    server_key = _load_or_create_key(server_key_path)
    identity_csr = _load_or_create_csr(
        identity_key, identity_csr_path, agent_host, display_name, version
    )
    server_csr = _load_or_create_csr(
        server_key, server_csr_path, agent_host, display_name, version
    )
    functions = _public_skill_functions(current_env)

    payload: dict[str, Any] = {
        "agentDisplayName": display_name,
        "agentHost": agent_host,
        "version": version,
        "identityCsrPEM": _b64_pem(
            identity_csr.public_bytes(serialization.Encoding.PEM)
        ),
        "serverCsrPEM": _b64_pem(server_csr.public_bytes(serialization.Encoding.PEM)),
        "endpoints": _build_endpoints(
            current_env,
            agent_host=agent_host,
            functions=functions,
        ),
    }
    if description:
        payload["agentDescription"] = description

    payload_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    curl_example = (
        "curl -X POST "
        f"{_ans_base_url(current_env)}/v1/agents/register "
        "-H 'Authorization: sso-key <KEY>:<SECRET>' "
        "-H 'Content-Type: application/json' "
        f"--data '@{payload_path}'"
    )
    summary = {
        "ans_api_base_url": _ans_base_url(current_env),
        "agentHost": agent_host,
        "version": version,
        "payload_path": str(payload_path),
        "identity_key_path": str(identity_key_path),
        "identity_csr_path": str(identity_csr_path),
        "server_key_path": str(server_key_path),
        "server_csr_path": str(server_csr_path),
        "curl_example": curl_example,
        "credential_status": credential_status(current_env),
    }
    validation = validate_registration_payload(payload)
    summary["validation"] = validation
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    return {"payload": payload, "summary": summary, "validation": validation}


def _http_error_hint(status_code: int) -> dict[str, Any] | None:
    if status_code == 401:
        return {
            "error_type": "authentication_failed",
            "message": "GoDaddy rejected the supplied API credentials.",
            "next_steps": [
                "Confirm GODADDY_API_KEY and GODADDY_API_SECRET are configured in the runtime environment.",
                "Confirm the credentials belong to the intended production or OTE environment.",
            ],
        }
    if status_code == 403:
        return {
            "error_type": "authorization_failed",
            "message": (
                "GoDaddy received credentials but did not authorize this ANS request. "
                "This usually means the key/secret pair is invalid for ANS, lacks ANS access, "
                "or is for the wrong production/OTE environment."
            ),
            "next_steps": [
                "Check whether GODADDY_ANS_ENV points at the intended environment.",
                "Confirm the configured GoDaddy credentials are enabled for ANS registry APIs.",
                "If using curl or another client, verify the Authorization header is not using placeholders.",
            ],
        }
    return None


def _json_request(
    method: str,
    path: str,
    *,
    body: Any | None = None,
    query: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    current_env = _env(env)
    url = f"{_ans_base_url(current_env)}{path}"
    if query:
        encoded = urllib.parse.urlencode(query, doseq=True)
        if encoded:
            url = f"{url}?{encoded}"

    data = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {
        "Authorization": _auth_header(current_env),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update({key: value for key, value in headers.items() if value})

    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            return {
                "status_code": response.status,
                "url": url,
                "body": json.loads(response_body) if response_body else None,
            }
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(response_body)
        except Exception:
            parsed = {"raw": response_body}
        result = {
            "status_code": exc.code,
            "url": url,
            "body": parsed,
            "error": response_body,
        }
        hint = _http_error_hint(exc.code)
        if hint:
            result["diagnostic"] = hint
        return result


def _query_variants(query_text: str) -> list[str]:
    normalized = " ".join(str(query_text).lower().strip().split())
    if not normalized:
        return []
    variants = [normalized]
    if normalized.endswith("s") and len(normalized) > 1:
        variants.append(normalized[:-1])
    else:
        variants.append(f"{normalized}s")
    variants.append(normalized.replace("-", " "))
    variants.append(normalized.replace("_", " "))
    return list(dict.fromkeys(variant for variant in variants if variant))


def _broadened_query_terms(query_text: str) -> list[str]:
    normalized = " ".join(str(query_text).lower().strip().split())
    if " " in normalized or len(normalized) < 5:
        return []

    candidates: list[str] = []
    if len(normalized) >= 6:
        candidates.append(normalized[:4])
    candidates.append(normalized[:3])
    return [
        candidate
        for candidate in dict.fromkeys(candidates)
        if candidate and candidate not in _query_variants(normalized)
    ]


def _search_body_container(body: Any) -> tuple[list[Any] | None, str | None]:
    if isinstance(body, list):
        return body, None
    if isinstance(body, dict):
        for key in ("agents", "items", "results", "registrations"):
            value = body.get(key)
            if isinstance(value, list):
                return value, key
    return None, None


def _matches_query(item: Any, query_text: str) -> bool:
    variants = _query_variants(query_text)
    if not variants:
        return True

    try:
        haystack = json.dumps(item, sort_keys=True).lower()
    except TypeError:
        haystack = str(item).lower()
    return any(variant in haystack for variant in variants)


def _apply_query_filter(result: dict[str, Any], query_text: str | None) -> dict[str, Any]:
    if not query_text:
        return result

    body = result.get("body")
    container, container_key = _search_body_container(body)
    if container is None:
        result["query"] = {
            "text": query_text,
            "matched": 0,
            "total_examined": 0,
            "warning": "Could not locate a list container in the ANS search response body.",
        }
        return result

    filtered = [item for item in container if _matches_query(item, query_text)]
    query_meta = {
        "text": query_text,
        "matched": len(filtered),
        "total_examined": len(container),
    }

    if container_key is None:
        result["body"] = filtered
    elif isinstance(body, dict):
        new_body = dict(body)
        new_body[container_key] = filtered
        result["body"] = new_body

    result["query"] = query_meta
    return result


def _search_filters(
    *,
    agent_display_name: str | None = None,
    agent_host: str | None = None,
    version: str | None = None,
    protocol: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    status: list[str] | str | None = None,
) -> dict[str, Any]:
    request_query: dict[str, Any] = {}
    if agent_display_name:
        request_query["agentDisplayName"] = agent_display_name[:MAX_AGENT_DISPLAY_NAME_LENGTH]
    if agent_host:
        request_query["agentHost"] = _normalize_agent_host(agent_host)
    if version:
        request_query["version"] = version
    if protocol:
        normalized_protocol = protocol.strip().upper()
        if normalized_protocol not in VALID_PROTOCOLS:
            raise ValueError("protocol must be one of A2A, MCP, or HTTP-API.")
        request_query["protocol"] = normalized_protocol
    if limit is not None:
        request_query["limit"] = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    if offset is not None:
        request_query["offset"] = max(0, int(offset))
    if status:
        if isinstance(status, str):
            statuses = [status]
        else:
            statuses = [str(item) for item in status]
        normalized_statuses = [item.strip().upper() for item in statuses if item.strip()]
        invalid_statuses = [
            item for item in normalized_statuses if item not in VALID_AGENT_STATUSES
        ]
        if invalid_statuses:
            raise ValueError(
                "status must contain only PENDING_DNS, ACTIVE, DEPRECATED, REVOKED, or ALL."
            )
        request_query["status"] = (
            "ALL" if "ALL" in normalized_statuses else normalized_statuses
        )
    return request_query


def _item_key(item: Any) -> str:
    if not isinstance(item, dict):
        try:
            return json.dumps(item, sort_keys=True)
        except TypeError:
            return str(item)

    for key in ("agentId", "id", "agent_id"):
        value = item.get(key)
        if value:
            return f"id:{value}"

    host = item.get("agentHost") or item.get("host")
    version = item.get("version") or item.get("agentVersion")
    display_name = item.get("agentDisplayName") or item.get("displayName") or item.get("name")
    if host or version or display_name:
        return f"agent:{host or ''}:{version or ''}:{display_name or ''}"

    try:
        return json.dumps(item, sort_keys=True)
    except TypeError:
        return str(item)


def _response_items(result: dict[str, Any]) -> list[Any]:
    container, _container_key = _search_body_container(result.get("body"))
    return list(container or [])


def _loose_query_search(
    *,
    query: str,
    version: str | None = None,
    protocol: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    status: list[str] | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    result_limit = int(limit) if limit is not None else 20
    result_limit = max(1, min(result_limit, MAX_SEARCH_LIMIT))
    request_limit = max(result_limit, 20)
    base_filters = _search_filters(
        version=version,
        protocol=protocol,
        limit=request_limit,
        offset=offset,
        status=status,
    )

    merged: list[Any] = []
    seen: set[str] = set()
    request_summaries: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    used_terms: list[str] = []
    broadened = False

    def run_terms(terms: list[str], *, broadened_round: bool) -> None:
        nonlocal broadened
        if broadened_round:
            broadened = True
        for term in terms:
            if term not in used_terms:
                used_terms.append(term)
            for field in ("agentDisplayName", "agentHost"):
                request_query = dict(base_filters)
                request_query[field] = term
                response = _json_request("GET", "/v1/agents", query=request_query, env=env)
                responses.append(response)
                items = _response_items(response)
                request_summaries.append(
                    {
                        "field": field,
                        "term": term,
                        "status_code": response.get("status_code"),
                        "returned": len(items),
                    }
                )
                for item in items:
                    key = _item_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(item)

    primary_terms = _query_variants(query)
    run_terms(primary_terms, broadened_round=False)
    if not merged:
        fallback_terms = _broadened_query_terms(query)
        if fallback_terms:
            run_terms(fallback_terms, broadened_round=True)

    limited = merged[:result_limit]
    successful = [response for response in responses if 200 <= int(response.get("status_code", 0)) < 300]
    status_code = 200 if successful else int(responses[0].get("status_code", 500)) if responses else 500

    return {
        "status_code": status_code,
        "url": "multiple",
        "body": {"agents": limited},
        "query": {
            "text": query,
            "mode": "server_side_filters",
            "searched_fields": ["agentDisplayName", "agentHost"],
            "terms": used_terms,
            "broadened": broadened,
            "matched": len(limited),
            "total_examined": sum(summary["returned"] for summary in request_summaries),
            "request_count": len(request_summaries),
            "request_summaries": request_summaries,
            "note": (
                "Loose query searches use ANS API server-side display-name and host "
                "filters, then deduplicate results. Client-side substring filtering "
                "is only used when exact filters are supplied with query."
            ),
        },
    }


def register_agent(
    env: Mapping[str, str] | None = None,
    state_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    bundle = build_registration_bundle(env=env, state_dir=state_dir)
    validation = bundle["validation"]
    credentials = credential_status(env)
    if dry_run:
        return {
            "dry_run": True,
            "ready_to_submit": validation["valid"] and credentials["live_api_credentials_configured"],
            "bundle": bundle,
            "credential_status": credentials,
        }
    if not validation["valid"]:
        return {
            "dry_run": False,
            "submitted": False,
            "error_type": "invalid_registration_payload",
            "message": "Generated registration payload failed local validation.",
            "validation": validation,
            "bundle": bundle,
        }
    if not credentials["live_api_credentials_configured"]:
        return {
            "dry_run": False,
            "submitted": False,
            "error_type": "missing_credentials",
            "message": (
                "GODADDY_API_KEY and GODADDY_API_SECRET must be configured in the "
                "runtime environment before submitting live GoDaddy ANS API calls."
            ),
            "credential_status": credentials,
            "bundle": bundle,
        }
    response = _json_request(
        "POST",
        "/v1/agents/register",
        body=bundle["payload"],
        env=env,
    )
    return {
        "dry_run": False,
        "submitted": 200 <= int(response.get("status_code", 0)) < 300,
        "bundle": bundle,
        "credential_status": credentials,
        "response": response,
    }


def _base64_encoded_pem(value: str) -> str:
    stripped = str(value).strip()
    if stripped.startswith("-----BEGIN"):
        return _b64_pem(stripped.encode("utf-8"))
    return stripped


def search_agents(
    *,
    query: str | None = None,
    agent_display_name: str | None = None,
    agent_host: str | None = None,
    version: str | None = None,
    protocol: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    status: list[str] | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if query and not agent_display_name and not agent_host:
        return _loose_query_search(
            query=query,
            version=version,
            protocol=protocol,
            limit=limit,
            offset=offset,
            status=status,
            env=env,
        )

    request_query = _search_filters(
        agent_display_name=agent_display_name,
        agent_host=agent_host,
        version=version,
        protocol=protocol,
        limit=limit,
        offset=offset,
        status=status,
    )
    search_result = _json_request("GET", "/v1/agents", query=request_query, env=env)
    return _apply_query_filter(search_result, query)


def resolve_agent(
    agent_host: str,
    version: str | None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _json_request(
        "POST",
        "/v1/agents/resolution",
        body={"agentHost": _normalize_agent_host(agent_host), "version": version or ""},
        env=env,
    )


def get_agent(agent_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _json_request("GET", f"/v1/agents/{agent_id}", env=env)


def revoke_agent(
    agent_id: str,
    reason: str,
    comments: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized_reason = reason.strip().upper()
    if normalized_reason not in VALID_REVOCATION_REASONS:
        raise ValueError(
            "reason must be one of KEY_COMPROMISE, CESSATION_OF_OPERATION, "
            "AFFILIATION_CHANGED, SUPERSEDED, CERTIFICATE_HOLD, "
            "PRIVILEGE_WITHDRAWN, or AA_COMPROMISE."
        )
    body: dict[str, Any] = {"reason": normalized_reason}
    if comments:
        body["comments"] = comments[:200]
    return _json_request("POST", f"/v1/agents/{agent_id}/revoke", body=body, env=env)


def verify_acme(agent_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _json_request("POST", f"/v1/agents/{agent_id}/verify-acme", env=env)


def verify_dns(agent_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _json_request("POST", f"/v1/agents/{agent_id}/verify-dns", env=env)


def get_identity_certificates(agent_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _json_request("GET", f"/v1/agents/{agent_id}/certificates/identity", env=env)


def submit_identity_csr(
    agent_id: str,
    csr_pem: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _json_request(
        "POST",
        f"/v1/agents/{agent_id}/certificates/identity",
        body={"csrPEM": _base64_encoded_pem(csr_pem)},
        env=env,
    )


def get_server_certificates(agent_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _json_request("GET", f"/v1/agents/{agent_id}/certificates/server", env=env)


def submit_server_csr(
    agent_id: str,
    csr_pem: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _json_request(
        "POST",
        f"/v1/agents/{agent_id}/certificates/server",
        body={"csrPEM": _base64_encoded_pem(csr_pem)},
        env=env,
    )


def get_csr_status(
    agent_id: str,
    csr_id: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _json_request("GET", f"/v1/agents/{agent_id}/csrs/{csr_id}/status", env=env)


def get_events(
    *,
    provider_id: str | None = None,
    last_log_id: str | None = None,
    limit: int | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if provider_id:
        query["providerId"] = provider_id
    if last_log_id:
        query["lastLogId"] = last_log_id
    if limit is not None:
        query["limit"] = max(1, min(int(limit), MAX_EVENT_LIMIT))
    return _json_request("GET", "/v1/agents/events", query=query, env=env)


def _normalize_dns_record_type(record_type: str) -> str:
    normalized = str(record_type).strip().upper()
    if normalized not in VALID_DNS_RECORD_TYPES:
        raise ValueError("record_type must be one of A, AAAA, CNAME, MX, NS, SOA, SRV, or TXT.")
    return normalized


def _normalize_dns_record_name(name: str) -> str:
    stripped = str(name).strip().strip(".")
    if not stripped:
        raise ValueError("record name is required.")
    return "@" if stripped == "@" else stripped.lower()


def _dns_record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    data = record.get("data")
    if data is None or str(data).strip() == "":
        raise ValueError("each DNS record requires data.")
    payload["data"] = str(data).strip()

    for key in ("ttl", "priority", "port", "weight"):
        value = record.get(key)
        if value is not None:
            payload[key] = int(value)
    for key in ("protocol", "service"):
        value = record.get(key)
        if value is not None and str(value).strip():
            payload[key] = str(value).strip()
    return payload


def set_dns_records(
    *,
    domain: str,
    record_type: str,
    name: str,
    records: list[Mapping[str, Any]],
    shopper_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized_domain = _normalize_agent_host(domain)
    normalized_type = _normalize_dns_record_type(record_type)
    normalized_name = _normalize_dns_record_name(name)
    if not records:
        raise ValueError("records must contain at least one DNS record.")

    body = [_dns_record_payload(record) for record in records]
    encoded_domain = urllib.parse.quote(normalized_domain, safe="")
    encoded_type = urllib.parse.quote(normalized_type, safe="")
    encoded_name = urllib.parse.quote(normalized_name, safe="")
    headers = {"X-Shopper-Id": shopper_id} if shopper_id else None
    return _json_request(
        "PUT",
        f"/v1/domains/{encoded_domain}/records/{encoded_type}/{encoded_name}",
        body=body,
        headers=headers,
        env=env,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("bootstrap", help="Generate ANS CSRs and a registration payload bundle.")
    register_parser = subparsers.add_parser(
        "register", help="Generate the ANS bundle and submit it to GoDaddy."
    )
    register_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate the registration payload without calling GoDaddy.",
    )

    search_parser = subparsers.add_parser("search", help="Search GoDaddy ANS agents.")
    search_parser.add_argument("--agent-display-name")
    search_parser.add_argument("--agent-host")
    search_parser.add_argument("--version")
    search_parser.add_argument("--protocol")
    search_parser.add_argument("--limit", type=int)
    search_parser.add_argument("--offset", type=int)
    search_parser.add_argument("--status", action="append")
    search_parser.add_argument("--query")

    resolve_parser = subparsers.add_parser(
        "resolve", help="Resolve an ANS agent by host and version."
    )
    resolve_parser.add_argument("--agent-host", required=True)
    resolve_parser.add_argument(
        "--version",
        default="",
        help="SemVer, range, '*', or empty string for latest.",
    )

    get_parser = subparsers.add_parser("get", help="Fetch one registered agent by id.")
    get_parser.add_argument("--agent-id", required=True)

    verify_acme_parser = subparsers.add_parser(
        "verify-acme", help="Trigger ANS ACME verification."
    )
    verify_acme_parser.add_argument("--agent-id", required=True)

    verify_dns_parser = subparsers.add_parser(
        "verify-dns", help="Trigger ANS DNS verification."
    )
    verify_dns_parser.add_argument("--agent-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            result = build_registration_bundle()
        elif args.command == "register":
            result = register_agent(dry_run=args.dry_run)
        elif args.command == "search":
            result = search_agents(
                query=args.query,
                agent_display_name=args.agent_display_name,
                agent_host=args.agent_host,
                version=args.version,
                protocol=args.protocol,
                limit=args.limit,
                offset=args.offset,
                status=args.status,
            )
        elif args.command == "resolve":
            result = resolve_agent(args.agent_host, args.version)
        elif args.command == "get":
            result = get_agent(args.agent_id)
        elif args.command == "verify-acme":
            result = verify_acme(args.agent_id)
        elif args.command == "verify-dns":
            result = verify_dns(args.agent_id)
        else:
            raise ValueError(f"Unsupported command: {args.command}")

        print(json.dumps(result, indent=2), file=sys.stdout)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
