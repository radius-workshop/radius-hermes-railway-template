#!/usr/bin/env python3
"""
Agent Server — A2A HTTP gateway and agent discovery endpoints.
Python/FastAPI/uvicorn replacement for the Bun/Hono TypeScript server.
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
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auth import get_did, get_did_document, issue_token, jwt_auth_dep, setup_auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-server")

_start_time = time.time()

# ——— Configuration ———

SKILLS_ROOT = os.environ.get("SKILLS_ROOT", "/data/.hermes/well-known-skills")


def _get_base_url() -> str:
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"]
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        return f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
    return f"http://localhost:{os.environ.get('PORT', '3000')}"


BASE_URL = _get_base_url()


def _did_web_to_base_url(did: str) -> Optional[str]:
    """Convert a did:web identifier to its HTTPS base URL."""
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

# ——— Skills index ———

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
    empty = json.dumps(
        {"$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json", "skills": []},
        indent=2,
    )
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

    return json.dumps(
        {"$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json", "skills": skills},
        indent=2,
    )


def _get_index() -> str:
    global _skills_cache, _cache_built_at
    now = time.time()
    if not _skills_cache or now - _cache_built_at > _CACHE_TTL:
        _skills_cache = _build_index()
        _cache_built_at = now
    return _skills_cache


# ——— App setup ———

@asynccontextmanager
async def lifespan(app: FastAPI):
    await setup_auth(BASE_URL)
    port = os.environ.get("PORT", "3000")
    logger.info(f"[agent-server] Listening on port {port}, BASE_URL={BASE_URL}")
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def _cors_skills(request: Request, call_next):
    if request.url.path.startswith("/.well-known/agent-skills/"):
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                },
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response
    return await call_next(request)


# ——— Skills routes ———

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
        logger.error(f"Failed reading skill file {skill_path}: {e}")
        return PlainTextResponse("Internal Server Error", status_code=500)


# ——— Discovery routes ———

@app.get("/.well-known/did.json")
async def did_document_route():
    doc = get_did_document()
    if not doc:
        return JSONResponse({"error": "Not ready"}, status_code=503)
    return Response(
        content=json.dumps(doc),
        media_type="application/did+json",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=600"},
    )


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
    return JSONResponse(
        registration,
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=60"},
    )


@app.get("/.well-known/agent-card.json")
async def agent_card():
    agent_name = os.environ.get("AGENT_NAME", "Hermes Agent")
    did = get_did()
    webhook_enabled = os.environ.get("WEBHOOK_ENABLED", "").lower() == "true"
    skills_index = json.loads(_get_index())
    skills = [
        {
            "id": s["name"],
            "name": s["name"],
            "description": s.get("description", ""),
            "tags": [],
            "input_modes": ["text/plain"],
            "output_modes": ["text/plain"],
        }
        for s in skills_index.get("skills", [])
    ]
    card: dict = {
        "name": agent_name,
        "description": os.environ.get("AGENT_DESCRIPTION", f"{agent_name} — AI agent powered by Hermes"),
        "version": "1.0.0",
        "provider": {"name": agent_name, "url": BASE_URL, **({"did": did} if did else {})},
        "supported_interfaces": [
            {"protocol_binding": "JSONRPC", "url": f"{BASE_URL}/a2a", "protocol_version": "1.0"}
        ],
        "capabilities": {"streaming": False, "push_notifications": webhook_enabled, "extended_agent_card": False},
        "default_input_modes": ["text/plain"],
        "default_output_modes": ["text/plain"],
        "skills": skills,
        "security_schemes": {
            "bearer_jwt": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "DID-signed JWT (ES256K / did:key). Obtain via POST /token or sign with your own did:key identity.",
            }
        },
    }
    return JSONResponse(card, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=60"})


# ——— Landing page ———

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Hermes Agent</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #000; color: #fff; font-family: monospace;
      font-size: 14px; line-height: 1.6; padding: 48px 32px; max-width: 720px;
    }
    h1 { font-size: 18px; font-weight: normal; margin-bottom: 32px; }
    section { margin-bottom: 32px; }
    .label { color: #555; margin-bottom: 8px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.08em; }
    .value { color: #fff; word-break: break-all; }
    .dim { color: #555; }
    .skill { margin-bottom: 16px; }
    .skill-name { color: #fff; }
    .skill-desc { color: #888; margin-top: 2px; }
    a { color: #fff; text-decoration: none; border-bottom: 1px solid #333; }
    a:hover { border-color: #fff; }
    .error { color: #555; font-style: italic; }
    .fork { position: fixed; top: 48px; right: 32px; text-align: right; font-size: 11px; color: #555; line-height: 1.5; }
    .fork a { color: #555; border-bottom-color: #222; }
    .fork a:hover { color: #fff; border-color: #555; }
  </style>
</head>
<body>
  <div class="fork">
    <img src="https://railway.com/brand/logo-light.svg" alt="Railway" width="72"
         style="display:block;margin-left:auto;margin-bottom:6px;opacity:0.4;">
    clone &amp; deploy your own<br>
    <a href="https://github.com/radius-workshop/hermes-railway-template" target="_blank" rel="noopener">radius-workshop/hermes-railway-template</a>
  </div>
  <h1 id="agent-name">—</h1>
  <section id="section-wallet" style="display:none">
    <div class="label">wallet</div>
    <div class="value"><a id="wallet-address" href="#" target="_blank" rel="noopener"></a></div>
  </section>
  <section id="section-registry" style="display:none">
    <div class="label">erc-8004 identity registry</div>
    <div class="value"><a id="registry-address" href="#" target="_blank" rel="noopener"></a></div>
  </section>
  <section id="section-services" style="display:none">
    <div class="label">services</div>
    <div id="services-list"></div>
  </section>
  <section>
    <div class="label">skills</div>
    <div id="skills-list"><span class="dim">loading...</span></div>
  </section>
  <script>
    async function load() {
      try {
        const reg = await fetch('/.well-known/agent-registration.json').then(r => r.json());
        document.getElementById('agent-name').textContent = reg.name ?? 'Hermes Agent';
        if (reg.wallet) {
          const el = document.getElementById('wallet-address');
          el.textContent = reg.wallet;
          el.href = `https://testnet.radiustech.xyz/address/${reg.wallet}`;
          document.getElementById('section-wallet').style.display = '';
        }
        if (reg.identityRegistry) {
          const contractAddr = reg.identityRegistry.split(':').pop();
          const el = document.getElementById('registry-address');
          el.textContent = reg.identityRegistry;
          el.href = `https://testnet.radiustech.xyz/address/${contractAddr}`;
          document.getElementById('section-registry').style.display = '';
        }
        const services = reg.services ?? {};
        const svcKeys = Object.keys(services);
        if (svcKeys.length) {
          document.getElementById('services-list').innerHTML = svcKeys.map(k =>
            `<div><span class="dim">${k}</span> <a href="${services[k]}" target="_blank" rel="noopener">${services[k]}</a></div>`
          ).join('');
          document.getElementById('section-services').style.display = '';
        }
      } catch (e) {
        document.getElementById('agent-name').textContent = 'Hermes Agent';
      }
      try {
        const idx = await fetch('/.well-known/agent-skills/index.json').then(r => r.json());
        const el = document.getElementById('skills-list');
        const skills = idx.skills ?? [];
        if (!skills.length) { el.innerHTML = '<span class="dim">no published skills</span>'; return; }
        el.innerHTML = skills.map(s => `
          <div class="skill">
            <div class="skill-name"><a href="${s.url}" target="_blank" rel="noopener">${s.name}</a></div>
            ${s.description ? `<div class="skill-desc">${s.description}</div>` : ''}
          </div>`).join('');
      } catch (e) {
        document.getElementById('skills-list').innerHTML = '<span class="error">failed to load skills</span>';
      }
    }
    load();
  </script>
</body>
</html>""")


# ——— Debug endpoint (optional) ———

if os.environ.get("DEBUG_SKILLS") == "1":
    @app.get("/debug/skills")
    async def debug_skills(auth: dict = Depends(jwt_auth_dep)):
        try:
            skills_root = Path(SKILLS_ROOT)
            return {
                "SKILLS_ROOT": SKILLS_ROOT,
                "BASE_URL": BASE_URL,
                "rootExists": Path("/data").exists(),
                "hermesExists": Path("/data/.hermes").exists(),
                "wellKnownSkillsExists": skills_root.exists(),
                "skillsList": [p.name for p in skills_root.iterdir()] if skills_root.exists() else [],
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# ——— A2A endpoint ———

@app.post("/a2a")
async def handle_a2a(request: Request, auth: dict = Depends(jwt_auth_dep)):
    webhook_secret = os.environ.get("WEBHOOK_SECRET")
    if not webhook_secret:
        return JSONResponse({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32603, "message": "Webhook not configured on this agent"},
        }, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    if body.get("jsonrpc") != "2.0" or not body.get("method"):
        return JSONResponse({
            "jsonrpc": "2.0", "id": body.get("id"),
            "error": {"code": -32600, "message": "Invalid Request"},
        }, status_code=400)

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method != "message/send":
        return JSONResponse({
            "jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": -32004, "message": "This operation is not supported"},
        })

    message = params.get("message")
    if not message:
        return JSONResponse({
            "jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": -32602, "message": "Invalid params: missing message"},
        })

    parts = message.get("parts") or []
    text = "\n".join(p["text"] for p in parts if isinstance(p.get("text"), str)).strip()
    if not text:
        return JSONResponse({
            "jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": -32602, "message": "Invalid params: no text content in message parts"},
        })

    issuer_did = auth.get("issuer")
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

    sig = hmac.new(
        webhook_secret.encode("utf-8"),
        webhook_payload.encode("utf-8"),
        "sha256",
    ).hexdigest()
    webhook_port = os.environ.get("WEBHOOK_PORT", "8644")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            webhook_res = await client.post(
                f"http://localhost:{webhook_port}/webhooks/a2a",
                content=webhook_payload,
                headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
            )
        if not webhook_res.is_success:
            logger.error(f"[a2a] Hermes webhook returned {webhook_res.status_code}")
            return JSONResponse({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32603, "message": f"Webhook delivery failed: HTTP {webhook_res.status_code}"},
            }, status_code=502)
    except Exception as e:
        logger.error(f"[a2a] Hermes webhook delivery failed: {e}")
        return JSONResponse({
            "jsonrpc": "2.0", "id": rpc_id,
            "error": {
                "code": -32603,
                "message": "Could not reach agent backend — ensure WEBHOOK_ENABLED=true and WEBHOOK_SECRET is set",
            },
        }, status_code=503)

    return JSONResponse({
        "jsonrpc": "2.0", "id": rpc_id,
        "result": {
            "id": task_id,
            "context_id": context_id,
            "status": {"state": "TASK_STATE_SUBMITTED", "timestamp_ms": int(time.time() * 1000)},
        },
    })


# ——— Token exchange ———

@app.post("/token")
async def token_exchange(request: Request):
    api_key = os.environ.get("JWT_API_KEY")
    if not api_key:
        return JSONResponse({"error": "Not found"}, status_code=404)
    provided = request.headers.get("X-Api-Key")
    if not provided or provided != api_key:
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


# ——— Health check ———

@app.get("/health")
async def health(auth: dict = Depends(jwt_auth_dep)):
    return {"status": "ok", "uptime": int(time.time() - _start_time)}


# ——— Entry point ———

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
