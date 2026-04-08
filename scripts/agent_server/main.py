#!/usr/bin/env python3
"""
Agent Server — A2A HTTP gateway and agent discovery endpoints.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import httpx
import uvicorn
from a2a.types import (
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    JSONParseError,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCSuccessResponse,
    MethodNotFoundError,
)
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a2a_bridge import A2ABridge
from auth import get_did, get_did_document, issue_token, jwt_auth_dep, setup_auth
from hermes_client import HermesClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-server")

_start_time = time.time()

SKILLS_ROOT = os.environ.get("SKILLS_ROOT", "/data/.hermes/well-known-skills")


def _get_base_url() -> str:
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"]
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        return f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
    return f"http://localhost:{os.environ.get('PORT', '3000')}"


BASE_URL = _get_base_url()
A2A_PUBLIC_URL = os.environ.get("A2A_PUBLIC_URL", BASE_URL)
A2A_MODE = os.environ.get("A2A_MODE", "auto").lower()
HERMES_URL = os.environ.get("HERMES_URL", "http://127.0.0.1:8642")
HERMES_MODEL = os.environ.get("HERMES_MODEL", "hermes-agent")
HERMES_TIMEOUT = float(os.environ.get("HERMES_TIMEOUT", "120"))

_hermes_client: Optional[HermesClient] = None
_a2a_bridge: Optional[A2ABridge] = None


def _parse_allowed_roots() -> list[Path]:
    raw = os.environ.get("A2A_FILE_SERVE_PATHS", "")
    if not raw.strip():
        return []
    roots: list[Path] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        path = Path(item).expanduser().resolve()
        if path.exists() and path.is_dir():
            roots.append(path)
    return roots


def _direct_available() -> bool:
    return bool(_a2a_bridge and os.environ.get("HERMES_API_KEY"))


def _resolve_mode(method: str) -> str:
    if A2A_MODE == "direct":
        return "direct"
    if A2A_MODE == "delegated":
        return "delegated"
    # auto
    if method in {"message/send", "message/stream"} and _direct_available():
        return "direct"
    return "delegated"


def _rpc_error_response(rpc_id, error_obj: JSONRPCError, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": error_obj.model_dump(by_alias=True, exclude_none=True)},
        status_code=status_code,
    )


def _rpc_success_response(rpc_id, result) -> JSONResponse:
    payload = JSONRPCSuccessResponse(id=rpc_id, result=result)
    return JSONResponse(payload.model_dump(by_alias=True, exclude_none=True))


def _did_web_to_base_url(did: str) -> Optional[str]:
    if not isinstance(did, str) or not did.startswith("did:web:"):
        return None
    did_path = did.split("#", 1)[0][8:]
    if not did_path:
        return None
    parts = did_path.split(":")
    host = unquote(parts[0]).replace("%3A", ":")
    if len(parts) == 1:
        return f"https://{host}"
    return f"https://{host}/{'/'.join(parts[1:])}"


def _is_published(skill_md: str) -> bool:
    if not skill_md.startswith("---"):
        return False
    end = skill_md.find("---", 3)
    if end < 0:
        return False
    return bool(re.search(r"\npublished:\s*true\s*\n", skill_md[3:end]))


def _parse_description(skill_md: str) -> str:
    if not skill_md.startswith("---"):
        return ""
    end = skill_md.find("---", 3)
    if end < 0:
        return ""
    fm = skill_md[3:end]
    block = re.search(r"\ndescription:\s*>\n((?:[ \t]+.+\n?)+)", fm)
    if block:
        return re.sub(r"[ \t]+", " ", block.group(1)).strip()
    inline = re.search(r'\ndescription:\s*["\']?(.+?)["\']?\s*\n', fm)
    if inline:
        return inline.group(1).strip()
    return ""


_skills_cache: Optional[str] = None
_cache_built_at: float = 0
_CACHE_TTL = 60.0


def _build_index() -> str:
    skills_root = Path(SKILLS_ROOT)
    empty = json.dumps({"$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json", "skills": []}, indent=2)
    if not skills_root.exists():
        return empty
    try:
        entries = sorted(p.name for p in skills_root.iterdir() if p.is_dir())
    except Exception:
        return empty

    skills = []
    for entry in entries:
        skill_path = skills_root / entry / "SKILL.md"
        if not skill_path.exists():
            continue
        try:
            content = skill_path.read_text(encoding="utf-8")
            if not _is_published(content):
                continue
            digest = "sha256:" + hashlib.sha256(skill_path.read_bytes()).hexdigest()
            skills.append({
                "name": entry,
                "type": "skill-md",
                "description": _parse_description(content),
                "url": f"{BASE_URL}/.well-known/agent-skills/{entry}/SKILL.md",
                "digest": digest,
            })
        except Exception:
            continue

    return json.dumps({"$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json", "skills": skills}, indent=2)


def _get_index() -> str:
    global _skills_cache, _cache_built_at
    now = time.time()
    if not _skills_cache or now - _cache_built_at > _CACHE_TTL:
        _skills_cache = _build_index()
        _cache_built_at = now
    return _skills_cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _hermes_client, _a2a_bridge
    await setup_auth(BASE_URL)
    if os.environ.get("HERMES_API_KEY"):
        _hermes_client = HermesClient(
            base_url=HERMES_URL,
            api_key=os.environ["HERMES_API_KEY"],
            model=HERMES_MODEL,
            timeout=HERMES_TIMEOUT,
        )
        _a2a_bridge = A2ABridge(_hermes_client, _parse_allowed_roots(), A2A_PUBLIC_URL)
    logger.info("[agent-server] Listening on port %s, BASE_URL=%s, A2A_MODE=%s", os.environ.get("PORT", "3000"), BASE_URL, A2A_MODE)
    yield
    if _hermes_client:
        await _hermes_client.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def _cors_skills(request: Request, call_next):
    if request.url.path.startswith("/.well-known/agent-skills/"):
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response
    return await call_next(request)


@app.api_route("/.well-known/agent-skills/index.json", methods=["GET", "HEAD"])
async def skills_index(request: Request):
    body = _get_index()
    headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "public, max-age=60"}
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)
    return Response(content=body, headers=headers)


@app.api_route("/.well-known/agent-skills/{name}/SKILL.md", methods=["GET", "HEAD"])
async def get_skill(request: Request, name: str):
    if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", name):
        return PlainTextResponse("Not Found", status_code=404)
    skill_path = Path(SKILLS_ROOT) / name / "SKILL.md"
    if not skill_path.exists():
        return PlainTextResponse("Not Found", status_code=404)
    try:
        raw = skill_path.read_bytes()
        if not _is_published(raw.decode("utf-8")):
            return PlainTextResponse("Not Found", status_code=404)
        headers = {"Cache-Control": "public, max-age=300", "Content-Length": str(len(raw))}
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers)
        return Response(content=raw, media_type="text/markdown; charset=utf-8", headers=headers)
    except Exception as e:
        logger.error("Failed reading skill file %s: %s", skill_path, e)
        return PlainTextResponse("Internal Server Error", status_code=500)


@app.get("/.well-known/did.json")
async def did_document_route():
    doc = get_did_document()
    if not doc:
        return JSONResponse({"error": "Not ready"}, status_code=503)
    return Response(content=json.dumps(doc), media_type="application/did+json", headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=600"})


@app.get("/.well-known/agent-registration.json")
async def agent_registration():
    wallet_address = os.environ.get("RADIUS_WALLET_ADDRESS")
    agent_name = os.environ.get("AGENT_NAME", "Hermes Agent")
    registration: dict = {
        "schemaVersion": "1.0",
        "name": agent_name,
        "x402Support": True,
        "trustSchemes": ["reput"],
        "identityRegistry": "eip155:72344:0x5cd923Ce1244d5498Bf3f9E0F3a374C2567F1A31",
        "services": {
            "rpc": "https://rpc.radiustech.xyz",
            "rpcTestnet": "https://rpc.testnet.radiustech.xyz",
            "faucet": "https://network.radiustech.xyz/api/v1/faucet/doc",
            "faucetTestnet": "https://testnet.radiustech.xyz/api/v1/faucet/doc",
        },
    }
    if wallet_address:
        registration["wallet"] = wallet_address
        registration["owner"] = wallet_address
    did = get_did()
    if did:
        registration["did"] = did
    return JSONResponse(registration, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=60"})


@app.get("/.well-known/agent-card.json")
async def agent_card():
    agent_name = os.environ.get("AGENT_NAME", "Hermes Agent")
    did = get_did()
    webhook_enabled = os.environ.get("WEBHOOK_ENABLED", "").lower() == "true"
    skills_index = json.loads(_get_index())
    mode = A2A_MODE
    direct = _direct_available()
    streaming = mode == "direct" or (mode == "auto" and direct)
    skills = [{
        "id": s["name"],
        "name": s["name"],
        "description": s.get("description", ""),
        "tags": ["a2a-direct", "a2a-delegated"],
        "input_modes": ["text/plain"],
        "output_modes": ["text/plain"],
    } for s in skills_index.get("skills", [])]
    card: dict = {
        "name": agent_name,
        "description": os.environ.get("AGENT_DESCRIPTION", f"{agent_name} — AI agent powered by Hermes"),
        "version": "1.1.0",
        "provider": {"name": agent_name, "url": BASE_URL, **({"did": did} if did else {})},
        "supported_interfaces": [{"protocol_binding": "JSONRPC", "url": f"{BASE_URL}/a2a", "protocol_version": "1.0"}],
        "capabilities": {
            "streaming": streaming,
            "push_notifications": webhook_enabled,
            "extended_agent_card": False,
            "a2a_modes": [mode] if mode != "auto" else ["direct", "delegated"],
        },
        "default_input_modes": ["text/plain"],
        "default_output_modes": ["text/plain"],
        "skills": skills,
        "security_schemes": {
            "bearer_jwt": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "DID-signed JWT (ES256K). Obtain via POST /token or sign with your own DID identity.",
            }
        },
    }
    return JSONResponse(card, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=60"})


@app.get("/")
async def index():
    return HTMLResponse("""<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Hermes Agent</title></head><body style='background:#000;color:#fff;font-family:monospace;padding:48px'>
<div style='position:fixed;top:48px;right:32px;text-align:right;color:#888;font-size:11px'>clone &amp; deploy your own<br><a style='color:#bbb' href='https://github.com/radius-workshop/radius-hermes-railway-template' target='_blank' rel='noopener'>radius-workshop/radius-hermes-railway-template</a></div>
<h1>Hermes Agent</h1><p>Discovery: <a style='color:#bbb' href='/.well-known/agent-card.json'>agent-card</a></p></body></html>""")


async def _handle_delegated(rpc_id, message: dict, issuer_did: str | None):
    webhook_secret = os.environ.get("WEBHOOK_SECRET")
    if not webhook_secret:
        return _rpc_error_response(rpc_id, InternalError(message="Webhook not configured on this agent"), status_code=503)

    parts = message.get("parts") or []
    text = "\n".join(p["text"] for p in parts if isinstance(p.get("text"), str)).strip()
    if not text:
        return _rpc_error_response(rpc_id, InvalidParamsError(message="Invalid params: no text content in message parts"))

    task_id = str(uuid.uuid4())
    context_id = message.get("context_id", task_id)
    issuer_did_url = _did_web_to_base_url(issuer_did) if issuer_did else None
    webhook_payload = json.dumps({
        "text": text,
        "context_id": context_id,
        "task_id": task_id,
        **({"issuer_did": issuer_did} if issuer_did else {}),
        **({"issuer_did_url": issuer_did_url} if issuer_did_url else {}),
    })
    sig = hmac.new(webhook_secret.encode("utf-8"), webhook_payload.encode("utf-8"), "sha256").hexdigest()
    webhook_port = os.environ.get("WEBHOOK_PORT", "8644")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            webhook_res = await client.post(
                f"http://localhost:{webhook_port}/webhooks/a2a",
                content=webhook_payload,
                headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
            )
        if not webhook_res.is_success:
            return _rpc_error_response(
                rpc_id,
                InternalError(message=f"Webhook delivery failed: HTTP {webhook_res.status_code}"),
                status_code=502,
            )
    except Exception:
        return _rpc_error_response(
            rpc_id,
            InternalError(message="Could not reach agent backend — ensure WEBHOOK_ENABLED=true and WEBHOOK_SECRET is set"),
            status_code=503,
        )

    return _rpc_success_response(
        rpc_id,
        {"id": task_id, "context_id": context_id, "status": {"state": "TASK_STATE_SUBMITTED", "timestamp_ms": int(time.time() * 1000)}},
    )


@app.post("/a2a")
async def handle_a2a(request: Request, auth: dict = Depends(jwt_auth_dep)):
    try:
        body = await request.json()
    except Exception:
        return _rpc_error_response(None, JSONParseError(message="Parse error"), status_code=400)

    try:
        parsed_request = JSONRPCRequest.model_validate(body)
        body = parsed_request.model_dump(by_alias=True, exclude_none=True)
    except Exception:
        return _rpc_error_response(body.get("id") if isinstance(body, dict) else None, InvalidRequestError(), status_code=400)

    if body.get("jsonrpc") != "2.0" or not body.get("method"):
        return _rpc_error_response(body.get("id"), InvalidRequestError(message="Invalid Request"), status_code=400)

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    message = params.get("message")
    if not message:
        return _rpc_error_response(rpc_id, InvalidParamsError(message="Invalid params: missing message"))

    mode = _resolve_mode(method)
    if method not in {"message/send", "message/stream"}:
        return _rpc_error_response(rpc_id, MethodNotFoundError(message="This operation is not supported"))

    if mode == "delegated":
        if method == "message/stream":
            return _rpc_error_response(rpc_id, MethodNotFoundError(message="message/stream is only supported in direct mode"))
        return await _handle_delegated(rpc_id, message, auth.get("issuer"))

    if not _a2a_bridge:
        return _rpc_error_response(
            rpc_id, InternalError(message="Direct A2A bridge is unavailable; set HERMES_API_KEY"), status_code=503
        )

    try:
        if method == "message/send":
            send_payload = await _a2a_bridge.handle_send(rpc_id, message)
            return _rpc_success_response(rpc_id, send_payload.get("result"))

        async def _sse():
            try:
                async for event in _a2a_bridge.stream_events(rpc_id, message):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception:
                logger.exception("Direct A2A streaming failure")
                err = {"jsonrpc": "2.0", "id": rpc_id, "error": InternalError(message="Internal processing error").model_dump(exclude_none=True)}
                yield f"data: {json.dumps(err)}\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")
    except ValueError:
        return _rpc_error_response(rpc_id, InvalidParamsError(message="Invalid params"))
    except Exception:
        logger.exception("Direct A2A failure")
        return _rpc_error_response(rpc_id, InternalError(message="Internal processing error"))


@app.get("/files/{file_path:path}")
async def serve_file(file_path: str, auth: dict = Depends(jwt_auth_dep)):
    requested = Path(file_path)
    for root in _parse_allowed_roots():
        candidate = (root / requested).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.exists() and candidate.is_file():
            return Response(content=candidate.read_bytes(), media_type="application/octet-stream")
    for root in _parse_allowed_roots():
        candidate = (root / requested).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return PlainTextResponse("Not Found", status_code=404)
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/token")
async def token_exchange(request: Request):
    api_key = os.environ.get("JWT_API_KEY")
    if not api_key:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if request.headers.get("X-Api-Key") != api_key:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    sub = "client"
    try:
        body = await request.json()
        if isinstance(body.get("sub"), str):
            sub = body["sub"]
    except Exception:
        pass
    token = await issue_token(sub)
    return JSONResponse({"token": token})


@app.get("/health")
async def health(auth: dict = Depends(jwt_auth_dep)):
    return {
        "status": "ok",
        "uptime": int(time.time() - _start_time),
        "a2a_mode": A2A_MODE,
        "direct_ready": _direct_available(),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
