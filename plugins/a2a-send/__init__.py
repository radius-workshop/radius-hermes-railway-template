import json
import logging
import os
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse

import requests


logger = logging.getLogger("a2a-send")


def _get_base_url() -> str:
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"].rstrip("/")
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        return f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}".rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', '3000')}"


def _did_web_to_base_url(did: str) -> str | None:
    if not isinstance(did, str) or not did.startswith("did:web:"):
        return None
    did_path = did.split("#", 1)[0][8:]
    if not did_path:
        return None
    parts = did_path.split(":")
    host = parts[0].replace("%3A", ":")
    if len(parts) == 1:
        return f"https://{host}"
    return f"https://{host}/{'/'.join(parts[1:])}"


def _normalize_target(agent: str | None) -> str:
    if not agent or not str(agent).strip():
        peer = os.environ.get("A2A_PEER_URL", "").strip()
        if peer:
            return peer.rstrip("/")
        raise ValueError("No agent target provided and A2A_PEER_URL is not set.")

    value = str(agent).strip().rstrip("/")
    if value.startswith("did:web:"):
        base_url = _did_web_to_base_url(value)
        if not base_url:
            raise ValueError(f"Unsupported DID target: {value}")
        return base_url

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        for suffix in (
            "/.well-known/agent-card.json",
            "/.well-known/agent-registration.json",
            "/.well-known/agent-skills/index.json",
            "/.well-known/did.json",
            "/a2a",
            "/token",
        ):
            if path.endswith(suffix):
                trimmed = path[: -len(suffix)].rstrip("/")
                return f"{base}{trimmed}" if trimmed else base
        return value

    raise ValueError(
        "Invalid agent target. Expected a base URL, did:web DID, or a discovery document URL."
    )


def _generate_self_signed_token() -> dict:
    result = subprocess.run(
        [sys.executable, "/app/scripts/agent_server/gen_jwt.py"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Failed to generate A2A token")
    return json.loads(result.stdout)


def _exchange_api_key_for_token(base_url: str, api_key: str, subject: str) -> str:
    response = requests.post(
        f"{base_url}/token",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        json={"sub": subject},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("token")
    if not token:
        raise RuntimeError("Remote /token response did not include a token")
    return token


def _log_event(event: str, **fields) -> None:
    details = " ".join(
        f"{key}={json.dumps(value, separators=(',', ':')) if isinstance(value, (dict, list)) else value}"
        for key, value in fields.items()
        if value is not None
    )
    logger.info("[a2a-outbound] event=%s%s%s", event, " " if details else "", details)


def register(ctx):
    schema = {
        "name": "send_a2a_message",
        "description": (
            "Send a task to another A2A-compatible agent with built-in sender-side correlation logging. "
            "Returns the remote result plus local correlation ids including rpc_id, a2a_message_id, "
            "context_id, and the remote a2a_task_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Target agent base URL, did:web DID, or discovery URL. Defaults to A2A_PEER_URL if set.",
                },
                "task": {
                    "type": "string",
                    "description": "Task text to send to the remote agent.",
                },
                "context_id": {
                    "type": "string",
                    "description": "Optional existing context_id to continue an A2A thread.",
                },
                "api_key": {
                    "type": "string",
                    "description": "Optional remote JWT exchange key. Defaults to A2A_PEER_API_KEY if set.",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional subject for /token exchange. Defaults to hermes.",
                },
            },
            "required": ["task"],
        },
    }

    def handle(params, **kwargs):
        params = params or {}
        task = str(params.get("task") or "").strip()
        if not task:
            return "Error sending A2A message: missing task text"

        try:
            base_url = _normalize_target(params.get("agent"))
            api_key = str(params.get("api_key") or os.environ.get("A2A_PEER_API_KEY", "")).strip()
            subject = str(params.get("subject") or "hermes").strip() or "hermes"
            rpc_id = str(uuid.uuid4())
            message_id = str(uuid.uuid4())
            context_id = str(params.get("context_id") or "").strip() or str(uuid.uuid4())

            if api_key:
                token = _exchange_api_key_for_token(base_url, api_key, subject)
                auth_mode = "token_exchange"
                caller_did = None
            else:
                token_result = _generate_self_signed_token()
                token = token_result["token"]
                caller_did = token_result.get("did")
                auth_mode = "self_signed_jwt"

            payload = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "ROLE_USER",
                        "id": message_id,
                        "message_id": message_id,
                        "context_id": context_id,
                        "parts": [{"text": task}],
                    }
                },
            }

            _log_event(
                "submit",
                remote_agent=base_url,
                rpc_id=rpc_id,
                a2a_message_id=message_id,
                context_id=context_id,
                auth_mode=auth_mode,
                caller_did=caller_did,
                prompt_chars=len(task),
            )

            started = time.perf_counter()
            response = requests.post(
                f"{base_url}/a2a",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            response.raise_for_status()
            response_json = response.json()
            result = response_json.get("result") or {}

            _log_event(
                "result",
                remote_agent=base_url,
                rpc_id=rpc_id,
                a2a_message_id=message_id,
                context_id=result.get("context_id") or context_id,
                a2a_task_id=result.get("id"),
                remote_status=getattr(response, "status_code", None),
                duration_ms=duration_ms,
                task_state=((result.get("status") or {}).get("state")),
            )

            return json.dumps(
                {
                    "remote_agent": base_url,
                    "auth_mode": auth_mode,
                    "caller_did": caller_did,
                    "rpc_id": rpc_id,
                    "a2a_message_id": message_id,
                    "context_id": result.get("context_id") or context_id,
                    "a2a_task_id": result.get("id"),
                    "duration_ms": duration_ms,
                    "response": response_json,
                },
                indent=2,
            )
        except Exception as err:
            _log_event(
                "error",
                remote_agent=params.get("agent") or os.environ.get("A2A_PEER_URL"),
                error=str(err),
            )
            return f"Error sending A2A message: {err}"

    ctx.register_tool(
        name="send_a2a_message",
        toolset="a2a-send",
        schema=schema,
        handler=handle,
    )
