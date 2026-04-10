#!/usr/bin/env python3
"""
Agent Server — A2A HTTP gateway and agent discovery endpoints.
"""
import asyncio
import hashlib
import html
import hmac
import json
import logging
import os
import re
import subprocess
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
from hermes_client import HermesClient, HermesUnavailableError, HermesUpstreamError
from logging_utils import (
    clear_request_context,
    configure_logging,
    get_request_context,
    log_event,
    set_request_context,
    update_request_context,
)
from url_utils import get_base_url

configure_logging()
logger = logging.getLogger("agent-server")

_start_time = time.time()

SKILLS_ROOT = os.environ.get("SKILLS_ROOT", "/data/.hermes/well-known-skills")


BASE_URL = get_base_url()
A2A_PUBLIC_URL = os.environ.get("A2A_PUBLIC_URL", BASE_URL)
A2A_MODE = os.environ.get("A2A_MODE", "auto").lower()
HERMES_URL = os.environ.get("HERMES_URL", "http://127.0.0.1:8642")
A2A_BRIDGE_MODEL = os.environ.get("A2A_BRIDGE_MODEL", "hermes-agent")
HERMES_TIMEOUT = float(os.environ.get("HERMES_TIMEOUT", "120"))

_hermes_client: Optional[HermesClient] = None
_a2a_bridge: Optional[A2ABridge] = None
_wallet_summary_cache: Optional[dict] = None
_wallet_summary_built_at: float = 0
_WALLET_SUMMARY_TTL = 45.0


def _hermes_api_key() -> Optional[str]:
    return os.environ.get("HERMES_API_KEY") or os.environ.get("API_SERVER_KEY")


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
    return bool(_a2a_bridge and _hermes_api_key())


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


def _request_id(request: Request) -> str:
    return request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or str(uuid.uuid4())


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _is_observable_path(path: str) -> bool:
    return path in {"/a2a", "/token", "/health"} or path.startswith("/files/")


def _radius_address_file() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    return Path(hermes_home) / ".radius" / "address"


def _wallet_address() -> Optional[str]:
    address = os.environ.get("RADIUS_WALLET_ADDRESS", "").strip()
    if address:
        return address
    address_file = _radius_address_file()
    if address_file.exists():
        try:
            return address_file.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None
    return None


def _build_wallet_summary() -> dict:
    address = _wallet_address()
    if not address:
        return {"address": None, "sbc": None, "rusd": None, "error": "wallet_unavailable"}

    try:
        result = subprocess.run(
            [sys.executable, "/app/scripts/radius/balance.py", address],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "balance_failed").strip()
            raise RuntimeError(error)
        data = json.loads(result.stdout)
        return {
            "address": data.get("address") or address,
            "sbc": data.get("sbc"),
            "rusd": data.get("rusd"),
            "error": None,
        }
    except Exception as exc:
        return {"address": address, "sbc": None, "rusd": None, "error": str(exc)}


async def _get_wallet_summary() -> tuple[dict, bool]:
    global _wallet_summary_cache, _wallet_summary_built_at
    now = time.time()
    if _wallet_summary_cache and now - _wallet_summary_built_at <= _WALLET_SUMMARY_TTL:
        return _wallet_summary_cache, True

    started = time.perf_counter()
    summary = await asyncio.to_thread(_build_wallet_summary)
    _wallet_summary_cache = summary
    _wallet_summary_built_at = now
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    level = logging.WARNING if summary.get("error") else logging.INFO
    log_event(
        logger,
        level,
        "Homepage wallet summary refreshed",
        event="homepage.wallet_summary",
        cache_hit=False,
        address=summary.get("address"),
        sbc=summary.get("sbc"),
        rusd=summary.get("rusd"),
        wallet_error=summary.get("error"),
        duration_ms=duration_ms,
    )
    return summary, False


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
    hermes_api_key = _hermes_api_key()
    if hermes_api_key:
        _hermes_client = HermesClient(
            base_url=HERMES_URL,
            api_key=hermes_api_key,
            model=A2A_BRIDGE_MODEL,
            timeout=HERMES_TIMEOUT,
        )
        _a2a_bridge = A2ABridge(_hermes_client, _parse_allowed_roots(), A2A_PUBLIC_URL)
    log_event(
        logger,
        logging.INFO,
        "Agent server started",
        event="server.startup",
        port=os.environ.get("PORT", "3000"),
        base_url=BASE_URL,
        a2a_mode=A2A_MODE,
        direct_ready=_direct_available(),
        hermes_url=HERMES_URL,
    )
    yield
    if _hermes_client:
        await _hermes_client.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def _cors_skills(request: Request, call_next):
    if request.url.path == "/a2a" and request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
            },
        )
    if request.url.path.startswith("/.well-known/agent-skills/"):
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response
    return await call_next(request)


@app.middleware("http")
async def _request_logging(request: Request, call_next):
    request_id = _request_id(request)
    token = set_request_context(
        request_id=request_id,
        path=request.url.path,
        method=request.method,
    )
    started = time.perf_counter()

    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-Id"] = request_id
        if _is_observable_path(request.url.path):
            log_event(
                logger,
                logging.INFO,
                "HTTP request completed",
                event="http.request",
                path=request.url.path,
                method=request.method,
                status_code=response.status_code,
                duration_ms=duration_ms,
                client_ip=_client_ip(request),
            )
        return response
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event(
            logger,
            logging.ERROR,
            "HTTP request failed",
            event="http.request",
            path=request.url.path,
            method=request.method,
            duration_ms=duration_ms,
            client_ip=_client_ip(request),
            unhandled_exception=True,
        )
        raise
    finally:
        clear_request_context(token)


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
        log_event(logger, logging.ERROR, "Failed reading skill file", event="skills.read_error", skill_name=name, skill_path=str(skill_path), error=str(e))
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
    webhook_enabled = bool(os.environ.get("WEBHOOK_SECRET"))
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
    agent_name = os.environ.get("AGENT_NAME", "Hermes Agent")
    did = get_did() or "Unavailable"
    wallet_summary, cache_hit = await _get_wallet_summary()
    skills_index = json.loads(_get_index())
    if cache_hit:
        log_event(
            logger,
            logging.INFO,
            "Homepage wallet summary served from cache",
            event="homepage.wallet_summary",
            cache_hit=True,
            address=wallet_summary.get("address"),
            sbc=wallet_summary.get("sbc"),
            rusd=wallet_summary.get("rusd"),
            wallet_error=wallet_summary.get("error"),
        )

    wallet_address = wallet_summary.get("address") or "Unavailable"
    sbc_balance = wallet_summary.get("sbc") or "Unavailable"
    rusd_balance = wallet_summary.get("rusd") or "Unavailable"
    wallet_error = wallet_summary.get("error")
    explorer_link = f"https://testnet.radiustech.xyz/address/{wallet_address}" if wallet_summary.get("address") else "https://testnet.radiustech.xyz"

    links = [
        ("Agent Card", "/.well-known/agent-card.json"),
        ("Agent Skills", "/.well-known/agent-skills/index.json"),
        ("DID Document", "/.well-known/did.json"),
        ("ERC-8004 Registration", "/.well-known/agent-registration.json"),
    ]
    links_html = "".join(
        f"<a class='linkcard' href='{html.escape(href)}'><span>{html.escape(label)}</span><strong>{html.escape(href)}</strong></a>"
        for label, href in links
    )
    published_skills = skills_index.get("skills", [])
    skills_html = "".join(
        (
            "<li>"
            f"<strong>{html.escape(skill.get('name', 'unknown'))}</strong>: "
            f"{html.escape(skill.get('description') or 'Published skill')}"
            "</li>"
        )
        for skill in published_skills
    )
    if not skills_html:
        skills_html = "<li><strong>No published skills</strong>: This agent has not exposed any public skills yet.</li>"
    wallet_note = (
        f"<p class='note'>Wallet data unavailable: {html.escape(wallet_error)}</p>"
        if wallet_error
        else "<p class='note'>Wallet balances refresh automatically with a short cache to keep the public page fast.</p>"
    )

    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='UTF-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1.0'>
  <title>{html.escape(agent_name)}</title>
  <style>
    :root {{
      --bg: #081018;
      --bg2: #102131;
      --card: rgba(12, 24, 36, 0.82);
      --line: rgba(255,255,255,0.12);
      --text: #f2f7fb;
      --muted: #a7bacb;
      --accent: #73f0b3;
      --accent2: #7bc6ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(115,240,179,0.16), transparent 32%),
        radial-gradient(circle at top right, rgba(123,198,255,0.18), transparent 30%),
        linear-gradient(180deg, var(--bg), var(--bg2));
      padding: 28px;
    }}
    .wrap {{ max-width: 1080px; margin: 0 auto; }}
    .hero {{
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 18px;
      align-items: stretch;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 22px;
      backdrop-filter: blur(10px);
      box-shadow: 0 18px 60px rgba(0,0,0,0.22);
    }}
    h1 {{ margin: 0 0 8px; font-size: clamp(34px, 5vw, 56px); line-height: 0.95; }}
    p {{ margin: 0; color: var(--muted); line-height: 1.55; }}
    .eyebrow {{
      display: inline-block;
      margin-bottom: 12px;
      color: var(--accent);
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-size: 12px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .stat {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: rgba(255,255,255,0.03);
    }}
    .label {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .value {{ font-size: 22px; color: var(--text); word-break: break-word; }}
    .value.small {{ font-size: 13px; line-height: 1.5; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
      margin-top: 18px;
    }}
    .links {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}
    .linkcard {{
      display: block;
      text-decoration: none;
      color: inherit;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      background: rgba(255,255,255,0.02);
    }}
    .linkcard span {{ display: block; color: var(--muted); margin-bottom: 4px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }}
    .linkcard strong {{ color: var(--accent2); font-size: 13px; word-break: break-all; }}
    .list {{ margin: 14px 0 0; padding-left: 18px; color: var(--muted); }}
    .list li {{ margin: 8px 0; }}
    .note {{ margin-top: 14px; font-size: 12px; }}
    .repo {{ margin-top: 16px; font-size: 12px; }}
    .repo a, .value a {{ color: var(--accent2); }}
    @media (max-width: 860px) {{
      .hero, .grid, .stats {{ grid-template-columns: 1fr; }}
      body {{ padding: 16px; }}
    }}
  </style>
</head>
<body>
  <div class='wrap'>
    <section class='hero'>
      <div class='card'>
        <span class='eyebrow'>Radius Hermes Agent</span>
        <h1>{html.escape(agent_name)}</h1>
        <p>Public discovery profile for this agent. Use the links below to inspect its A2A surface, DID identity, and published skills.</p>
        <div class='stats'>
          <div class='stat'>
            <span class='label'>DID</span>
            <div class='value small'>{html.escape(did)}</div>
          </div>
          <div class='stat'>
            <span class='label'>EVM Address</span>
            <div class='value small'><a href='{html.escape(explorer_link)}' target='_blank' rel='noopener'>{html.escape(wallet_address)}</a></div>
          </div>
          <div class='stat'>
            <span class='label'>SBC Balance</span>
            <div class='value'>{html.escape(sbc_balance)}</div>
          </div>
          <div class='stat'>
            <span class='label'>RUSD Balance</span>
            <div class='value'>{html.escape(rusd_balance)}</div>
          </div>
        </div>
        {wallet_note}
        <p class='repo'>Deploy your own: <a href='https://github.com/radius-workshop/radius-hermes-railway-template' target='_blank' rel='noopener'>radius-workshop/radius-hermes-railway-template</a></p>
      </div>
      <div class='card'>
        <span class='eyebrow'>Discovery</span>
        <p>These endpoints are public and intended for operators, agent browsers, and other A2A-compatible systems.</p>
        <div class='links'>{links_html}</div>
      </div>
    </section>

    <section class='grid'>
      <div class='card'>
        <span class='eyebrow'>Published Skills</span>
        <p>This list is rendered from the public skills index and updates automatically as published skills change.</p>
        <ul class='list'>{skills_html}</ul>
      </div>
      <div class='card'>
        <span class='eyebrow'>What To Inspect</span>
        <ul class='list'>
          <li><strong>Agent Card</strong> for supported interfaces and auth scheme</li>
          <li><strong>DID Document</strong> for the agent's public signing key</li>
          <li><strong>Agent Registration</strong> for ERC-8004 and wallet identity</li>
          <li><strong>Skills Index</strong> for the published capability surface</li>
        </ul>
      </div>
    </section>
  </div>
</body>
</html>"""
    )


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
    update_request_context(rpc_id=rpc_id, context_id=context_id, issuer_did=issuer_did, a2a_mode="delegated", a2a_task_id=task_id)
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
            log_event(
                logger,
                logging.ERROR,
                "Delegated A2A webhook failed",
                event="a2a.delegated",
                outcome="error",
                rpc_id=rpc_id,
                context_id=context_id,
                a2a_task_id=task_id,
                issuer_did=issuer_did,
                upstream_status=webhook_res.status_code,
            )
            return _rpc_error_response(
                rpc_id,
                InternalError(message=f"Webhook delivery failed: HTTP {webhook_res.status_code}"),
                status_code=502,
            )
    except Exception:
        log_event(
            logger,
            logging.ERROR,
            "Delegated A2A webhook unavailable",
            event="a2a.delegated",
            outcome="error",
            rpc_id=rpc_id,
            context_id=context_id,
            a2a_task_id=task_id,
            issuer_did=issuer_did,
            webhook_port=webhook_port,
        )
        return _rpc_error_response(
            rpc_id,
            InternalError(message="Could not reach agent backend — ensure WEBHOOK_ENABLED=true and WEBHOOK_SECRET is set"),
            status_code=503,
        )

    log_event(
        logger,
        logging.INFO,
        "Delegated A2A task submitted",
        event="a2a.delegated",
        outcome="submitted",
        rpc_id=rpc_id,
        context_id=context_id,
        a2a_task_id=task_id,
        issuer_did=issuer_did,
        prompt_chars=len(text),
    )

    return _rpc_success_response(
        rpc_id,
        {"id": task_id, "context_id": context_id, "status": {"state": "TASK_STATE_SUBMITTED", "timestamp_ms": int(time.time() * 1000)}},
    )


def _hermes_error_response(rpc_id, exc: Exception) -> JSONResponse:
    if isinstance(exc, HermesUnavailableError):
        log_event(logger, logging.WARNING, "Direct A2A cannot reach Hermes backend", event="a2a.direct", outcome="error", rpc_id=rpc_id, hermes_error=str(exc), error_type="unavailable")
        return _rpc_error_response(
            rpc_id,
            InternalError(message="Hermes backend is unreachable. Check HERMES_URL and HERMES_API_KEY/API_SERVER_KEY."),
            status_code=503,
        )
    if isinstance(exc, HermesUpstreamError):
        log_event(logger, logging.WARNING, "Direct A2A Hermes upstream returned an error", event="a2a.direct", outcome="error", rpc_id=rpc_id, hermes_error=str(exc), error_type="upstream")
        return _rpc_error_response(
            rpc_id,
            InternalError(message=str(exc)),
            status_code=502,
        )
    log_event(logger, logging.ERROR, "Direct A2A failure", event="a2a.direct", outcome="error", rpc_id=rpc_id, error_type=type(exc).__name__, exc_info=True)
    return _rpc_error_response(rpc_id, InternalError(message="Internal processing error"))


@app.post("/a2a")
async def handle_a2a(request: Request, auth: dict = Depends(jwt_auth_dep)):
    try:
        body = await request.json()
    except Exception:
        log_event(logger, logging.WARNING, "A2A request body could not be parsed", event="a2a.request", outcome="rejected", rejection_reason="json_parse_error")
        return _rpc_error_response(None, JSONParseError(message="Parse error"), status_code=400)

    try:
        parsed_request = JSONRPCRequest.model_validate(body)
        body = parsed_request.model_dump(by_alias=True, exclude_none=True)
    except Exception:
        log_event(logger, logging.WARNING, "A2A request failed schema validation", event="a2a.request", outcome="rejected", rejection_reason="invalid_request")
        return _rpc_error_response(body.get("id") if isinstance(body, dict) else None, InvalidRequestError(), status_code=400)

    if body.get("jsonrpc") != "2.0" or not body.get("method"):
        log_event(logger, logging.WARNING, "A2A request missing required JSON-RPC fields", event="a2a.request", outcome="rejected", rejection_reason="invalid_jsonrpc_envelope", rpc_id=body.get("id"))
        return _rpc_error_response(body.get("id"), InvalidRequestError(message="Invalid Request"), status_code=400)

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    message = params.get("message")
    if not message:
        log_event(logger, logging.WARNING, "A2A request missing message payload", event="a2a.request", outcome="rejected", rejection_reason="missing_message", rpc_id=rpc_id, rpc_method=method)
        return _rpc_error_response(rpc_id, InvalidParamsError(message="Invalid params: missing message"))

    mode = _resolve_mode(method)
    message_id = message.get("message_id") if isinstance(message, dict) else None
    if isinstance(message, dict) and not message_id:
        message_id = message.get("id")
    context_id = message.get("context_id") if isinstance(message, dict) else None
    prompt_chars = 0
    if isinstance(message, dict):
        prompt_chars = sum(len(part.get("text", "")) for part in (message.get("parts") or []) if isinstance(part.get("text"), str))
    update_request_context(
        rpc_id=rpc_id,
        rpc_method=method,
        a2a_mode=mode,
        issuer_did=auth.get("issuer"),
        context_id=context_id,
        a2a_message_id=message_id,
    )
    started = time.perf_counter()
    log_event(
        logger,
        logging.INFO,
        "A2A request accepted",
        event="a2a.request",
        outcome="accepted",
        rpc_id=rpc_id,
        rpc_method=method,
        a2a_mode=mode,
        issuer_did=auth.get("issuer"),
        context_id=context_id,
        a2a_message_id=message_id,
        prompt_chars=prompt_chars,
    )
    if method not in {"message/send", "message/stream"}:
        log_event(logger, logging.WARNING, "A2A method not supported", event="a2a.request", outcome="rejected", rejection_reason="method_not_supported", rpc_id=rpc_id, rpc_method=method)
        return _rpc_error_response(rpc_id, MethodNotFoundError(message="This operation is not supported"))

    if mode == "delegated":
        if method == "message/stream":
            log_event(logger, logging.WARNING, "A2A streaming requires direct mode", event="a2a.request", outcome="rejected", rejection_reason="stream_requires_direct_mode", rpc_id=rpc_id, rpc_method=method)
            return _rpc_error_response(rpc_id, MethodNotFoundError(message="message/stream is only supported in direct mode"))
        return await _handle_delegated(rpc_id, message, auth.get("issuer"))

    if not _a2a_bridge:
        log_event(logger, logging.ERROR, "Direct A2A bridge unavailable", event="a2a.direct", outcome="error", rpc_id=rpc_id, rpc_method=method)
        return _rpc_error_response(
            rpc_id, InternalError(message="Direct A2A bridge is unavailable"), status_code=503
        )

    try:
        if method == "message/send":
            try:
                send_payload = await _a2a_bridge.handle_send(rpc_id, message)
                result = send_payload.get("result") or {}
                response_context = result.get("context_id")
                log_event(
                    logger,
                    logging.INFO,
                    "Direct A2A request completed",
                    event="a2a.direct",
                    outcome="completed",
                    rpc_id=rpc_id,
                    rpc_method=method,
                    a2a_task_id=result.get("id"),
                    context_id=response_context or context_id,
                    issuer_did=auth.get("issuer"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return _rpc_success_response(rpc_id, send_payload.get("result"))
            except HermesUnavailableError:
                # In auto mode, degrade to delegated task submission if direct mode backend is down.
                if A2A_MODE == "auto":
                    log_event(
                        logger,
                        logging.WARNING,
                        "Direct A2A falling back to delegated mode",
                        event="a2a.fallback",
                        rpc_id=rpc_id,
                        rpc_method=method,
                        issuer_did=auth.get("issuer"),
                        context_id=context_id,
                    )
                    return await _handle_delegated(rpc_id, message, auth.get("issuer"))
                raise

        stream_context = get_request_context()

        async def _sse():
            stream_token = set_request_context(**stream_context)
            try:
                async for event in _a2a_bridge.stream_events(rpc_id, message):
                    yield f"data: {json.dumps(event)}\n\n"
                log_event(
                    logger,
                    logging.INFO,
                    "Direct A2A stream completed",
                    event="a2a.direct_stream",
                    outcome="completed",
                    rpc_id=rpc_id,
                    rpc_method=method,
                    context_id=get_request_context().get("context_id"),
                    issuer_did=auth.get("issuer"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            except (HermesUnavailableError, HermesUpstreamError) as exc:
                if isinstance(exc, HermesUnavailableError):
                    log_event(logger, logging.WARNING, "Direct A2A streaming cannot reach Hermes backend", event="a2a.direct_stream", outcome="error", rpc_id=rpc_id, issuer_did=auth.get("issuer"), context_id=get_request_context().get("context_id"), hermes_error=str(exc), error_type="unavailable")
                    message_text = "Hermes backend is unreachable. Check HERMES_URL and HERMES_API_KEY/API_SERVER_KEY."
                else:
                    log_event(logger, logging.WARNING, "Direct A2A streaming Hermes upstream returned an error", event="a2a.direct_stream", outcome="error", rpc_id=rpc_id, issuer_did=auth.get("issuer"), context_id=get_request_context().get("context_id"), hermes_error=str(exc), error_type="upstream")
                    message_text = str(exc)
                err = {"jsonrpc": "2.0", "id": rpc_id, "error": InternalError(message=message_text).model_dump(exclude_none=True)}
                yield f"data: {json.dumps(err)}\n\n"
            except Exception:
                log_event(logger, logging.ERROR, "Direct A2A streaming failure", event="a2a.direct_stream", outcome="error", rpc_id=rpc_id, issuer_did=auth.get("issuer"), context_id=get_request_context().get("context_id"), exc_info=True)
                err = {"jsonrpc": "2.0", "id": rpc_id, "error": InternalError(message="Internal processing error").model_dump(exclude_none=True)}
                yield f"data: {json.dumps(err)}\n\n"
            finally:
                clear_request_context(stream_token)

        return StreamingResponse(_sse(), media_type="text/event-stream")
    except ValueError:
        log_event(logger, logging.WARNING, "A2A request failed parameter validation", event="a2a.request", outcome="rejected", rejection_reason="invalid_params", rpc_id=rpc_id, rpc_method=method)
        return _rpc_error_response(rpc_id, InvalidParamsError(message="Invalid params"))
    except Exception as exc:
        return _hermes_error_response(rpc_id, exc)


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
    api_key = os.environ.get("JWT_EXCHANGE_KEY") or os.environ.get("JWT_API_KEY")
    if not api_key:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if request.headers.get("X-Api-Key") != api_key:
        log_event(logger, logging.WARNING, "Token exchange rejected", event="token.exchange", outcome="unauthorized")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    sub = "client"
    try:
        body = await request.json()
        if isinstance(body.get("sub"), str):
            sub = body["sub"]
    except Exception:
        pass
    token = await issue_token(sub)
    log_event(logger, logging.INFO, "Token issued", event="token.exchange", outcome="issued", token_subject=sub)
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
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", access_log=False)
