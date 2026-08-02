"""KYA / PAY / KYA-PAY token minting plugin.

Companion to ``kya-spec``: one verifies KYA tokens received from peers,
the other mints KYA tokens this agent emits. Both delegate to the shared
``scripts/agent_server/kya_verify.py`` core so the same spec rules apply
in both directions.

Issuer identity:
  - The token's ``iss`` claim is this agent's public base URL.
  - The signing keypair is a persistent ES256/P-256 key under
    ``${HERMES_HOME}/.radius/kya/key.pem`` (generated lazily on first
    use).
  - The public half is served at ``/.well-known/jwks.json`` by the agent
    server, which is exactly where the spec tells verifiers to look.

The Radius wallet (secp256k1 / ES256K) is intentionally NOT reused for
KYA: KYA mandates ES256, which uses the P-256 curve. The two are not
interchangeable.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

# Resolve the canonical implementation. Mirrors plugins/gen-jwt and
# plugins/kya-spec — keeps script and plugin sharing one truth.
_AGENT_SERVER_DIR = os.path.join("/app", "scripts", "agent_server")
if os.path.isdir(_AGENT_SERVER_DIR) and _AGENT_SERVER_DIR not in sys.path:
    sys.path.insert(0, _AGENT_SERVER_DIR)

import kya_verify  # noqa: E402


def _default_issuer() -> str | None:
    """This agent's public base URL.

    Order of preference (matches scripts/agent_server/url_utils.py):
      1. ``PUBLIC_URL``
      2. ``https://${RAILWAY_PUBLIC_DOMAIN}``
      3. ``AGENT_PUBLIC_URL``
    """
    for key in ("PUBLIC_URL", "AGENT_PUBLIC_URL"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val.rstrip("/")
    domain = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if domain:
        return f"https://{domain.lstrip('https://').lstrip('http://').rstrip('/')}"
    return None


def _default_subject() -> str | None:
    """Default JWT ``sub`` for tokens this agent issues.

    Per spec, ``sub`` is the Subject Identifier and "must be pairwise
    unique within a given issuer". This agent's DID satisfies that under
    a single-issuer model.
    """
    did = (os.environ.get("AGENT_DID") or "").strip()
    if did:
        return did
    issuer = _default_issuer()
    if issuer:
        # did:web mirrors the issuer host — close enough for a default sub.
        host = issuer.split("://", 1)[-1].split("/", 1)[0]
        if host:
            return f"did:web:{host}"
    return None


def _default_agent_name() -> str:
    return (os.environ.get("AGENT_NAME") or "Hermes Agent").strip() or "Hermes Agent"


def _default_creation_ip() -> str | None:
    """Best-effort egress IP for the ``aid.creation_ip`` claim.

    Order of preference:
      1. ``AGENT_PUBLIC_IP`` env (operator-configured)
      2. Resolved A record of the public host (avoids an outbound HTTP call)
      3. None — caller must pass ``creation_ip`` explicitly.
    """
    explicit = (os.environ.get("AGENT_PUBLIC_IP") or "").strip()
    if explicit:
        return explicit
    issuer = _default_issuer()
    if not issuer:
        return None
    host = issuer.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if not host:
        return None
    try:
        info = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None
    for family, _, _, _, sockaddr in info:
        if family == socket.AF_INET:
            return sockaddr[0]
    for family, _, _, _, sockaddr in info:
        if family == socket.AF_INET6:
            return sockaddr[0]
    return None


def register(ctx):
    schema = {
        "name": "generate_kya_token",
        "description": (
            "Mint a signed KYA / PAY / KYA-PAY JWT this agent can present to a peer "
            "that wants to verify it under the KYAPay specification. The token is "
            "signed with this agent's persistent ES256 / P-256 keypair. Peers can "
            "verify via the public JWKS at this agent's /.well-known/jwks.json. "
            "The issuer URL, subject DID, agent name, and public IP for aid.creation_ip "
            "are inferred from the runtime if not supplied. Pair this tool with "
            "kya_validate_claims on the peer side."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "audience": {
                    "type": "string",
                    "description": "REQUIRED. Audience (aud) — uniquely identifies the seller agent receiving the token.",
                },
                "token_type": {
                    "type": "string",
                    "enum": ["kya", "pay", "kya-pay"],
                    "description": "Token profile to mint. Defaults to 'kya'.",
                },
                "subject": {
                    "type": "string",
                    "description": "Subject Identifier (sub). Defaults to this agent's DID.",
                },
                "issuer": {
                    "type": "string",
                    "description": "Issuer URL (iss). Defaults to this agent's PUBLIC_URL / RAILWAY_PUBLIC_DOMAIN.",
                },
                "ttl_seconds": {
                    "type": "integer",
                    "description": "Token lifetime in seconds. Defaults to 3600.",
                },
                "aid_name": {
                    "type": "string",
                    "description": "aid.name — agent's display/business name. Defaults to AGENT_NAME env or 'Hermes Agent'.",
                },
                "aid_creation_ip": {
                    "type": "string",
                    "description": "aid.creation_ip — public IP of the agent at token-mint time. Defaults to AGENT_PUBLIC_IP env, else resolves the public hostname's A record.",
                },
                "aid_source_ips": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "aid.source_ips — optional list of public IPs/CIDRs/domains this agent makes requests from.",
                },
                "hid": {
                    "type": "object",
                    "description": "Optional hid object (human identity). When provided, must include 'email'.",
                },
                "apd": {
                    "type": "object",
                    "description": "Optional apd object (agent platform identity). When provided, must include 'id' and 'name'.",
                },
                "pay": {
                    "type": "object",
                    "description": "Required for token_type='pay' or 'kya-pay'. PAY-related claims (amt, cur, stp, sti).",
                },
                "environment": {
                    "type": "string",
                    "description": "env claim, e.g. 'production' or 'sandbox'. Defaults to KYA_EXPECTED_ENVIRONMENT env if set.",
                },
                "seller_domain": {
                    "type": "string",
                    "description": "Optional sdm claim — seller domain associated with the audience.",
                },
                "originator": {
                    "type": "string",
                    "description": "Optional ori claim — URL of the token's originator.",
                },
                "seller_service_id": {
                    "type": "string",
                    "description": "Optional ssi claim — seller service ID this token was minted for.",
                },
                "buyer_tag": {
                    "type": "string",
                    "description": "Optional btg claim — opaque reference ID internal to the buyer.",
                },
                "extra_claims": {
                    "type": "object",
                    "description": "Optional non-reserved additional claims to merge into the payload.",
                },
                "verify_self": {
                    "type": "boolean",
                    "description": "When true (default), the minted token is run through verify_token using the in-process JWKS before being returned, to catch issuance bugs early.",
                },
            },
            "required": ["audience"],
        },
    }

    def handler(params, **kwargs):
        params = params or {}
        try:
            audience = params.get("audience")
            if not isinstance(audience, str) or not audience.strip():
                raise ValueError("audience is required and must be a non-empty string")
            audience = audience.strip()

            token_type = str(params.get("token_type") or "kya").strip().lower()
            if token_type not in {"kya", "pay", "kya-pay"}:
                raise ValueError("token_type must be one of: kya, pay, kya-pay")

            issuer = params.get("issuer") or _default_issuer()
            if not issuer:
                raise ValueError(
                    "issuer could not be determined; set PUBLIC_URL or RAILWAY_PUBLIC_DOMAIN, or pass 'issuer' explicitly"
                )
            subject = params.get("subject") or _default_subject()
            if not subject:
                raise ValueError(
                    "subject could not be determined; pass 'subject' explicitly or set AGENT_DID / PUBLIC_URL"
                )

            ttl_seconds = params.get("ttl_seconds")
            ttl = kya_verify.DEFAULT_TOKEN_TTL if ttl_seconds is None else int(ttl_seconds)

            # Build aid for kya / kya-pay.
            aid: dict[str, Any] | None = None
            if token_type in {"kya", "kya-pay"}:
                aid_name = (params.get("aid_name") or _default_agent_name()).strip()
                creation_ip = (
                    params.get("aid_creation_ip")
                    or _default_creation_ip()
                )
                if not creation_ip:
                    raise ValueError(
                        "aid_creation_ip could not be determined; pass it explicitly or set AGENT_PUBLIC_IP"
                    )
                aid = {"name": aid_name, "creation_ip": creation_ip}
                source_ips = params.get("aid_source_ips")
                if source_ips:
                    if not isinstance(source_ips, list) or not all(
                        isinstance(s, str) for s in source_ips
                    ):
                        raise ValueError("aid_source_ips must be a list of strings")
                    aid["source_ips"] = list(source_ips)

            hid = params.get("hid")
            apd = params.get("apd")
            pay = params.get("pay")
            environment = params.get("environment") or kya_verify.inbound_expected_environment()

            mint = kya_verify.mint_token(
                token_type=token_type,
                issuer=issuer,
                subject=subject,
                audience=audience,
                aid=aid,
                hid=hid,
                apd=apd,
                pay=pay,
                env=environment,
                seller_domain=params.get("seller_domain"),
                originator=params.get("originator"),
                seller_service_id=params.get("seller_service_id"),
                buyer_tag=params.get("buyer_tag"),
                extra_claims=params.get("extra_claims"),
                ttl_seconds=ttl,
            )

            response = {
                "token": mint["token"],
                "header": mint["header"],
                "payload": mint["payload"],
                "issuer": issuer,
                "kid": mint["kid"],
                "jwks_url": mint["jwks_url"],
                "header_name": kya_verify.inbound_header_name(),
            }

            verify_self = bool(params.get("verify_self", True))
            if verify_self:
                report = kya_verify.verify_token(
                    token_type=token_type,
                    token=mint["token"],
                    expected_audience=audience,
                    expected_environment=environment,
                    trusted_issuers=None,
                    enforce_audience=True,
                    enforce_replay_protection=False,
                    jwks_override=kya_verify.get_kya_jwks(),
                )
                response["self_verification"] = report.to_dict()

            return json.dumps(response, indent=2)
        except ValueError as err:
            return json.dumps({"error": str(err)}, indent=2)
        except Exception as err:  # defensive — unexpected crypto/key errors
            return json.dumps(
                {"error": f"Unexpected error while minting KYA token: {err}"},
                indent=2,
            )

    ctx.register_tool(
        name="generate_kya_token",
        toolset="kya-mint",
        schema=schema,
        handler=handler,
    )
