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
import yaml
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from a2a_sessions import A2ASessionStore
from a2a_bridge import A2ABridge
from auth import get_did, get_did_document, issue_token, jwt_auth_dep, setup_auth
from erc8004_registry import (
    MissingSelfRegistrationFields,
    build_self_registration,
    get_network_config,
    self_registration_missing_fields_error,
)
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

HERMES_HOME = os.environ.get("HERMES_HOME", "/data/.hermes")
CONFIG_PATH = Path(HERMES_HOME) / "config.yaml"
SKILLS_ROOT = os.environ.get("SKILLS_ROOT", "/data/.hermes/well-known-skills")
VENDORED_SKILLS_SOURCE = os.environ.get("VENDORED_SKILLS_SOURCE", "/app/vendor/radius-skills")
VENDORED_SKILLS_MANIFEST = Path(
    os.environ.get("VENDORED_SKILLS_MANIFEST", f"{HERMES_HOME}/vendored-skills.json")
)


BASE_URL = get_base_url()
A2A_PUBLIC_URL = os.environ.get("A2A_PUBLIC_URL", BASE_URL)
A2A_MODE = os.environ.get("A2A_MODE", "auto").lower()
HERMES_URL = os.environ.get("HERMES_URL", "http://127.0.0.1:8642")
A2A_BRIDGE_MODEL = os.environ.get("A2A_BRIDGE_MODEL", "hermes-agent")
HERMES_TIMEOUT = float(os.environ.get("HERMES_TIMEOUT", "120"))
A2A_SESSION_ROOT = Path(HERMES_HOME) / "a2a-sessions"
A2A_SESSION_TICK_SECONDS = float(os.environ.get("A2A_SESSION_TICK_SECONDS", "2.5"))

_hermes_client: Optional[HermesClient] = None
_a2a_bridge: Optional[A2ABridge] = None
_a2a_session_store = A2ASessionStore(A2A_SESSION_ROOT)
_a2a_session_worker: Optional[asyncio.Task] = None
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


def _internal_api_key() -> Optional[str]:
    return _hermes_api_key()


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


async def _internal_auth_dep(request: Request) -> None:
    expected = _internal_api_key()
    if not expected:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Internal API unavailable")
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {expected}":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Forbidden")


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
    return path in {"/a2a", "/token", "/health"} or path.startswith("/files/") or path.startswith("/internal/a2a/")


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


def _message_text(message: dict | None) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    text_parts = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text_parts.append(part["text"].strip())
    return "\n".join(chunk for chunk in text_parts if chunk).strip()


def _render_session_envelope(session: dict[str, Any], text: str) -> str:
    goal = str(session.get("goal") or "").strip()
    topic = str(session.get("topic") or "").strip()
    turn_number = int(session.get("turn_count") or 0) + 1
    max_turns = session.get("max_turns")
    turn_label = f"{turn_number}/{max_turns}" if max_turns not in (None, "") else f"{turn_number}/open"
    header = [
        "[A2A_SESSION]",
        f"session_id={session.get('session_id')}",
        f"context_id={session.get('context_id')}",
        f"turn={turn_label}",
        f"goal={goal or topic or 'ongoing collaboration'}",
        "reply_required=true",
        "[/A2A_SESSION]",
        "",
    ]
    return "\n".join(header) + text.strip()


def _build_dialogue_prompt(session: dict[str, Any]) -> str:
    max_turns = session.get("max_turns")
    next_turn = int(session.get("turn_count") or 0) + 1
    turn_label = f"{next_turn}/{max_turns}" if max_turns not in (None, "") else f"{next_turn}/open"
    goal = str(session.get("goal") or session.get("topic") or "Advance the shared objective").strip()
    transcript_lines = []
    for item in session.get("recent_messages") or []:
        speaker = "You" if item.get("speaker") == "local" else "Peer"
        transcript_lines.append(f"{speaker}: {item.get('text', '').strip()}")
    transcript = "\n".join(transcript_lines[-8:]) or "No prior turns recorded."
    return (
        "You are continuing an agent-to-agent work session.\n"
        f"Goal: {goal}\n"
        f"Next turn: {turn_label}\n"
        "Write the next message to the peer agent.\n"
        "Rules:\n"
        "- Advance the work instead of repeating prior points.\n"
        "- Keep the message under 140 words.\n"
        "- End with exactly one concrete question unless the work is complete.\n"
        "- Do not mention tools, JSON, metadata, or internal orchestration.\n"
        "- If the task is complete, say so clearly and ask for confirmation or the next work item.\n\n"
        "Recent transcript:\n"
        f"{transcript}\n"
    )


async def _exchange_api_key_for_token(base_url: str, api_key: str, subject: str = "hermes") -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/token",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"sub": subject},
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("token")
        if not token:
            raise RuntimeError("Remote /token response did not include a token")
        return token


async def _send_managed_a2a_turn(session: dict[str, Any], text: str) -> dict[str, Any]:
    remote_agent = str(session.get("remote_agent") or "").strip().rstrip("/")
    if not remote_agent:
        raise RuntimeError("Session has no remote_agent configured")

    api_key = _a2a_session_store.get_remote_api_key(str(session.get("session_id") or ""))
    if api_key:
        token = await _exchange_api_key_for_token(remote_agent, api_key)
        auth_mode = "token_exchange"
    else:
        token = await issue_token("hermes")
        auth_mode = "self_signed_jwt"

    rpc_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "id": message_id,
                "message_id": message_id,
                "context_id": session.get("context_id"),
                "parts": [{"text": _render_session_envelope(session, text)}],
            }
        },
    }

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            f"{remote_agent}/a2a",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.raise_for_status()
    response_json = response.json()

    log_event(
        logger,
        logging.INFO,
        "Managed A2A turn sent",
        event="a2a.session.outbound",
        session_id=session.get("session_id"),
        context_id=session.get("context_id"),
        remote_agent=remote_agent,
        auth_mode=auth_mode,
        rpc_id=rpc_id,
        a2a_message_id=message_id,
        duration_ms=duration_ms,
        task_state=((response_json.get("result") or {}).get("status") or {}).get("state"),
    )

    return {
        "session_id": session.get("session_id"),
        "context_id": (response_json.get("result") or {}).get("context_id") or session.get("context_id"),
        "a2a_message_id": message_id,
        "duration_ms": duration_ms,
        "response": response_json,
    }


async def _run_session_turn(session_id: str) -> None:
    session = _a2a_session_store.get_session(session_id)
    if not session or session.get("status") != "active" or session.get("next_action") != "compose_local_turn":
        return
    if not _hermes_client:
        _a2a_session_store.mark_error(session_id, "Direct Hermes client unavailable for managed A2A session")
        return
    try:
        prompt = _build_dialogue_prompt(session)
        composed = await _hermes_client.complete(
            messages=[{"role": "user", "content": prompt}],
            session_id=f"a2a-session:{session_id}",
        )
        composed = composed.strip()
        if not composed:
            raise RuntimeError("Managed A2A session produced an empty local turn")
        _a2a_session_store.record_local_turn(session_id, composed)
        session = _a2a_session_store.get_session(session_id)
        if not session:
            return
        result = await _send_managed_a2a_turn(session, composed)
        _a2a_session_store.record_outbound_result(result)
    except Exception as exc:
        _a2a_session_store.mark_error(session_id, str(exc))
        log_event(
            logger,
            logging.ERROR,
            "Managed A2A session turn failed",
            event="a2a.session.turn",
            session_id=session_id,
            error=str(exc),
        )


async def _session_worker_loop() -> None:
    while True:
        try:
            for session in _a2a_session_store.list_runnable_sessions():
                session_id = session.get("session_id")
                if not session_id:
                    continue
                _a2a_session_store.note_worker_claim(session_id, delay_seconds=max(A2A_SESSION_TICK_SECONDS * 4, 6.0))
                await _run_session_turn(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(logger, logging.ERROR, "Managed A2A session worker iteration failed", event="a2a.session.worker", error=str(exc))
        await asyncio.sleep(max(A2A_SESSION_TICK_SECONDS, 1.0))


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


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_config() -> dict[str, Any]:
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _scan_vendored_skills() -> dict[str, Any]:
    source = Path(VENDORED_SKILLS_SOURCE)
    skills: list[dict[str, Any]] = []
    roots: set[str] = set()
    if source.exists():
        for skill_md in sorted(source.rglob("SKILL.md")):
            skill_dir = skill_md.parent
            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception:
                content = ""
            root = str(skill_dir.parent)
            roots.add(root)
            skills.append(
                {
                    "name": skill_dir.name,
                    "path": str(skill_dir),
                    "root": root,
                    "published": _is_published(content),
                    "description": _parse_description(content),
                }
            )
    return {"source": str(source), "roots": sorted(roots), "skills": skills}


def _load_vendored_manifest() -> dict[str, Any]:
    try:
        return json.loads(VENDORED_SKILLS_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _list_local_skills() -> dict[str, Any]:
    hermes_skills_root = Path(HERMES_HOME) / "skills"
    flat = sorted(p.stem for p in hermes_skills_root.glob("*.md") if p.is_file())
    radius_bucket: list[str] = []
    radius_root = hermes_skills_root / "radius"
    if radius_root.exists():
        radius_bucket = sorted(p.name for p in radius_root.iterdir() if p.is_dir())
    return {
        "root": str(hermes_skills_root),
        "flat": flat,
        "radius_bucket": radius_bucket,
    }


def _debug_skills_payload() -> dict[str, Any]:
    config = _read_config()
    skills_cfg = config.get("skills") or {}
    external_dirs = skills_cfg.get("external_dirs") or []
    public_index = json.loads(_get_index())
    live_scan = _scan_vendored_skills()
    manifest = _load_vendored_manifest()
    well_known_root = Path(SKILLS_ROOT)
    well_known_dirs = []
    if well_known_root.exists():
        well_known_dirs = sorted(p.name for p in well_known_root.iterdir() if p.is_dir())
    return {
        "debug_enabled": _is_true(os.environ.get("DEBUG_SKILLS")),
        "config_path": str(CONFIG_PATH),
        "config_external_dirs": external_dirs,
        "vendored_manifest_path": str(VENDORED_SKILLS_MANIFEST),
        "vendored_manifest": manifest,
        "vendored_live_scan": live_scan,
        "local_skills": _list_local_skills(),
        "well_known_root": str(well_known_root),
        "well_known_skills": well_known_dirs,
        "public_index": public_index,
    }


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
    global _hermes_client, _a2a_bridge, _a2a_session_worker
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
    _a2a_session_worker = asyncio.create_task(_session_worker_loop(), name="a2a-session-worker")
    vendored_manifest = _load_vendored_manifest()
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
        a2a_session_root=str(A2A_SESSION_ROOT),
        a2a_session_tick_seconds=A2A_SESSION_TICK_SECONDS,
        vendored_skill_roots=vendored_manifest.get("roots", []),
        vendored_skill_names=[skill.get("name") for skill in vendored_manifest.get("skills", [])],
    )
    yield
    if _a2a_session_worker:
        _a2a_session_worker.cancel()
        try:
            await _a2a_session_worker
        except asyncio.CancelledError:
            pass
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


@app.get("/debug/skills")
async def debug_skills(auth: dict = Depends(jwt_auth_dep)):
    if not _is_true(os.environ.get("DEBUG_SKILLS")):
        return PlainTextResponse("Not Found", status_code=404)
    return JSONResponse(_debug_skills_payload(), headers={"Cache-Control": "no-store"})


@app.post("/internal/a2a/sessions/outbound")
async def internal_a2a_session_outbound(request: Request, _: None = Depends(_internal_auth_dep)):
    payload = await request.json()
    try:
        session = _a2a_session_store.create_or_update_outbound(payload if isinstance(payload, dict) else {})
    except ValueError as exc:
        log_event(
            logger,
            logging.WARNING,
            "Invalid outbound session payload",
            event="a2a.session.register_outbound_invalid_payload",
            error=str(exc),
        )
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    log_event(
        logger,
        logging.INFO,
        "Managed A2A session registered outbound turn",
        event="a2a.session.register_outbound",
        session_id=session.get("session_id"),
        context_id=session.get("context_id"),
        remote_agent=session.get("remote_agent"),
        auto_continue=session.get("auto_continue"),
    )
    return JSONResponse({"ok": True, "session": _a2a_session_store.serialize_for_response(session)})

@app.post("/internal/a2a/sessions/outbound-result")
async def internal_a2a_session_outbound_result(request: Request, _: None = Depends(_internal_auth_dep)):
    payload = await request.json()
    try:
        session = _a2a_session_store.record_outbound_result(payload if isinstance(payload, dict) else {})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not session:
        return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)
    return JSONResponse({"ok": True, "session": _a2a_session_store.serialize_for_response(session)})


@app.post("/internal/a2a/sessions/resolve")
async def internal_a2a_session_resolve(request: Request, _: None = Depends(_internal_auth_dep)):
    payload = await request.json()
    payload = payload if isinstance(payload, dict) else {}
    session = _a2a_session_store.find_active_session(
        remote_agent=payload.get("remote_agent"),
        context_id=payload.get("context_id"),
        origin_platform=((payload.get("origin") or {}).get("platform") if isinstance(payload.get("origin"), dict) else None),
        origin_chat_id=((payload.get("origin") or {}).get("chat_id") if isinstance(payload.get("origin"), dict) else None),
    )
    return JSONResponse({"ok": True, "session": _a2a_session_store.serialize_for_response(session)})


@app.get("/internal/a2a/sessions/{session_id}")
async def internal_a2a_session_get(session_id: str, _: None = Depends(_internal_auth_dep)):
    try:
        session = _a2a_session_store.get_session(session_id)
    except ValueError as exc:
        log_event(
            logger,
            logging.WARNING,
            "Invalid session lookup request",
            event="a2a.session.get.invalid_request",
            session_id=session_id,
            error=str(exc),
        )
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not session:
        return JSONResponse({"error": "Not Found"}, status_code=404)
    return JSONResponse(_a2a_session_store.serialize_for_response(session))


@app.get("/.well-known/did.json")
async def did_document_route():
    doc = get_did_document()
    if not doc:
        return JSONResponse({"error": "Not ready"}, status_code=503)
    return Response(content=json.dumps(doc), media_type="application/did+json", headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=600"})


@app.get("/.well-known/agent-registration.json")
async def agent_registration():
    try:
        registration = build_self_registration(
            get_network_config("testnet"),
            name=os.environ.get("AGENT_NAME"),
            description=os.environ.get("AGENT_DESCRIPTION"),
            image=os.environ.get("AGENT_IMAGE"),
            did=get_did(),
        )
    except MissingSelfRegistrationFields as err:
        return JSONResponse(
            self_registration_missing_fields_error(err),
            status_code=503,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=60",
            },
        )
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
    session = _a2a_session_store.find_by_context(context_id)
    webhook_payload = json.dumps({
        "text": text,
        "context_id": context_id,
        "task_id": task_id,
        **({"issuer_did": issuer_did} if issuer_did else {}),
        **({"issuer_did_url": issuer_did_url} if issuer_did_url else {}),
        **({"a2a_session_id": session.get("session_id")} if session else {}),
        **({"a2a_session_goal": session.get("goal") or session.get("topic")} if session else {}),
        **({"a2a_session_turn_count": session.get("turn_count")} if session else {}),
        **({"a2a_session_auto_continue": session.get("auto_continue")} if session else {}),
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
    managed_session = _a2a_session_store.find_by_context(context_id) if context_id else None
    if (
        managed_session
        and managed_session.get("controller_mode") == "local"
        and managed_session.get("auto_continue")
        and managed_session.get("status") == "active"
    ):
        inbound_text = _message_text(message if isinstance(message, dict) else None)
        session = _a2a_session_store.record_inbound_message(
            {
                "context_id": context_id,
                "issuer_did": auth.get("issuer"),
                "text": inbound_text,
            }
        )
        log_event(
            logger,
            logging.INFO,
            "Managed A2A session received remote turn",
            event="a2a.session.inbound",
            session_id=(session or managed_session).get("session_id"),
            context_id=context_id,
            issuer_did=auth.get("issuer"),
            prompt_chars=prompt_chars,
        )
        result = {
            "id": str(uuid.uuid4()),
            "context_id": context_id,
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp_ms": int(time.time() * 1000)},
            "message": {
                "role": "agent",
                "context_id": context_id,
                "parts": [{"type": "text", "text": "Turn received. Continuing the managed A2A session."}],
            },
        }
        return _rpc_success_response(rpc_id, result)
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
