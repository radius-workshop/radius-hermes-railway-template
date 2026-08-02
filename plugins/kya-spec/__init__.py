"""KYA / PAY / KYA-PAY token verification plugin.

Thin Hermes wrapper around ``scripts/agent_server/kya_verify.py`` — that
module owns the actual JWKS-fetch, signature verification, claim-shape
enforcement, and replay tracking, so the same logic is used by:

  - this LLM-facing tool (`kya_validate_claims`)
  - the A2A inbound middleware in ``scripts/agent_server/main.py``
  - the outbound minting plugin (`kya-mint`)

Keeping a single source of truth means a spec compliance fix lands in one
place and every consumer benefits.
"""

from __future__ import annotations

import json
import os
import sys

# Make the canonical implementation importable. The agent server bootstraps
# the same way (see scripts/agent_server/gen_jwt.py).
_AGENT_SERVER_DIR = os.path.join("/app", "scripts", "agent_server")
if os.path.isdir(_AGENT_SERVER_DIR) and _AGENT_SERVER_DIR not in sys.path:
    sys.path.insert(0, _AGENT_SERVER_DIR)

import kya_verify  # noqa: E402 — path mutation above is intentional


def register(ctx):
    schema = {
        "name": "kya_validate_claims",
        "description": (
            "Verify a KYA / PAY / KYA-PAY JWT issued under the KYAPay specification. "
            "Fetches the issuer's JWKS at {iss}/.well-known/jwks.json, matches the "
            "header kid, verifies the ES256 signature, and enforces spec-mandated "
            "claim shape, timestamps, audience binding, environment binding, and "
            "an optional trusted-issuer allowlist. Tracks (iss, jti) in memory to "
            "mitigate replay. Returns a structured validation report."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "token_type": {
                    "type": "string",
                    "enum": ["kya", "pay", "kya-pay"],
                    "description": "Token profile to validate against.",
                },
                "token": {
                    "type": "string",
                    "description": "Raw JWT string (header.payload.signature). Required.",
                },
                "expected_audience": {
                    "type": "string",
                    "description": (
                        "Audience value to enforce against payload.aud. Strongly "
                        "recommended — per the spec, recipients MUST validate aud. "
                        "If omitted, validation fails unless enforce_audience=false."
                    ),
                },
                "expected_environment": {
                    "type": "string",
                    "description": "Optional environment value to enforce against payload.env (e.g. 'production', 'sandbox').",
                },
                "trusted_issuers": {
                    "type": "string",
                    "description": (
                        "Comma-separated allowlist of issuer URLs to trust. "
                        "When omitted, falls back to the TRUSTED_KYA_ISSUERS env var. "
                        "When neither is set, any signature-verified issuer is accepted."
                    ),
                },
                "clock_skew_seconds": {
                    "type": "integer",
                    "description": "Clock skew tolerance applied symmetrically to iat and exp. Defaults to 60.",
                },
                "enforce_audience": {
                    "type": "boolean",
                    "description": "When true (default), missing expected_audience is an error. Set false only for diagnostic use.",
                },
                "enforce_replay_protection": {
                    "type": "boolean",
                    "description": "When true (default), reject tokens whose (iss, jti) has been seen before in this process.",
                },
            },
            "required": ["token_type", "token"],
        },
    }

    def handler(params, **kwargs):
        params = params or {}
        token_type = str(params.get("token_type") or "").strip().lower()
        if token_type not in {"kya", "pay", "kya-pay"}:
            return json.dumps(
                {"error": "token_type must be one of: kya, pay, kya-pay"}, indent=2
            )

        token = str(params.get("token") or "").strip()
        if not token:
            return json.dumps({"error": "token is required"}, indent=2)

        try:
            trusted = kya_verify.parse_trusted_issuers(params.get("trusted_issuers"))
        except ValueError as err:
            return json.dumps({"error": str(err)}, indent=2)

        clock_skew_raw = params.get("clock_skew_seconds")
        if clock_skew_raw is None:
            clock_skew = kya_verify.DEFAULT_CLOCK_SKEW
        else:
            try:
                clock_skew = max(0, int(clock_skew_raw))
            except (TypeError, ValueError):
                return json.dumps(
                    {"error": "clock_skew_seconds must be an integer"}, indent=2
                )

        expected_audience = params.get("expected_audience")
        if expected_audience is not None:
            expected_audience = str(expected_audience)
        expected_environment = params.get("expected_environment")
        if expected_environment is not None:
            expected_environment = str(expected_environment)

        enforce_audience = bool(params.get("enforce_audience", True))
        enforce_replay = bool(params.get("enforce_replay_protection", True))

        try:
            report = kya_verify.verify_token(
                token_type=token_type,
                token=token,
                expected_audience=expected_audience,
                expected_environment=expected_environment,
                trusted_issuers=trusted,
                clock_skew=clock_skew,
                enforce_audience=enforce_audience,
                enforce_replay_protection=enforce_replay,
            )
        except ValueError as err:
            return json.dumps({"error": str(err)}, indent=2)

        try:
            header, payload = kya_verify.jwt_split(token)
        except ValueError:
            header, payload = {}, {}

        return json.dumps(
            {
                **report.to_dict(),
                "header": header,
                "payload": payload,
            },
            indent=2,
        )

    ctx.register_tool(
        name="kya_validate_claims",
        toolset="kya-spec",
        schema=schema,
        handler=handler,
    )
