#!/usr/bin/env python3
"""
Agent Server — A2A HTTP gateway and agent discovery endpoints.
"""

import asyncio
import base64
import fcntl
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import httpx
import uvicorn
import yaml
from a2a.types import (
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    MethodNotFoundError,
)
from a2a.utils.errors import A2AError, JSON_RPC_ERROR_CODE_MAP
from fastapi import Depends, FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from erc8004_registry import (
    MissingSelfRegistrationFields,
    build_self_registration,
    get_network_config,
    self_registration_missing_fields_error,
)

from a2a_bridge import A2ABridge
from a2a_sessions import A2ASessionStore
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
from security_headers import apply_browser_security_headers, wallet_explorer_link
from url_utils import get_base_url

configure_logging()
logger = logging.getLogger("agent-server")

_start_time = time.time()

HERMES_HOME = os.environ.get("HERMES_HOME", "/data/.hermes")
REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
CONFIG_PATH = Path(HERMES_HOME) / "config.yaml"
SKILLS_ROOT = os.environ.get("SKILLS_ROOT", "/data/.hermes/well-known-skills")
RADIUS_SKILLS_DIR = os.environ.get("RADIUS_SKILLS_DIR", "/data/.hermes/external-skills/radius-skills")
VENDORED_SKILLS_SOURCE = os.environ.get("VENDORED_SKILLS_SOURCE", RADIUS_SKILLS_DIR)
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
_MOCK_MODE = os.environ.get("AGENT_SERVER_MOCK_DATA", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


class JSONRPCError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: int
    message: str
    data: dict[str, Any] | None = None


class JSONRPCRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jsonrpc: str
    id: Any | None = None
    method: str
    params: dict[str, Any] | None = None


class JSONRPCSuccessResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jsonrpc: str = "2.0"
    id: Any | None = None
    result: Any


class JSONParseError(A2AError):
    message = "Parse error"


def _jsonrpc_error_payload(error_obj: Any) -> dict[str, Any]:
    if hasattr(error_obj, "model_dump"):
        return error_obj.model_dump(by_alias=True, exclude_none=True)
    if isinstance(error_obj, A2AError):
        return JSONRPCError(
            code=JSON_RPC_ERROR_CODE_MAP.get(type(error_obj), -32603),
            message=getattr(error_obj, "message", str(error_obj) or "Internal error"),
            data=getattr(error_obj, "data", None),
        ).model_dump(exclude_none=True)
    return JSONRPCError(
        code=-32603,
        message=str(error_obj) or "Internal error",
    ).model_dump(exclude_none=True)


def _hermes_api_key() -> Optional[str]:
    return os.environ.get("HERMES_API_KEY") or os.environ.get("API_SERVER_KEY")


def _mock_wallet_summary() -> Optional[dict]:
    if not _MOCK_MODE:
        return None
    return {
        "address": os.environ.get(
            "MOCK_RADIUS_WALLET_ADDRESS", "0x4D8020F43A9EFb829DBe4Cb93cbb29d5B52aEc6b"
        ),
        "sbc": os.environ.get("MOCK_RADIUS_SBC_BALANCE", "40.05199"),
        "rusd": os.environ.get("MOCK_RADIUS_RUSD_BALANCE", "10.099815153377649216"),
        "error": os.environ.get("MOCK_RADIUS_WALLET_ERROR") or None,
    }


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


def _rpc_error_response(
    rpc_id, error_obj: JSONRPCError, status_code: int = 200
) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": _jsonrpc_error_payload(error_obj),
        },
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
    return (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or str(uuid.uuid4())
    )


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _is_observable_path(path: str) -> bool:
    return (
        path in {
            "/a2a",
            "/token",
            "/health",
            "/webhooks/github/radius-skills",
            "/internal/skills/status",
            "/internal/skills/sync",
        }
        or path.startswith("/files/")
        or path.startswith("/internal/a2a/")
    )


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
    mocked = _mock_wallet_summary()
    if mocked is not None:
        return mocked

    address = _wallet_address()
    if not address:
        return {
            "address": None,
            "sbc": None,
            "rusd": None,
            "error": "wallet_unavailable",
        }

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
    turn_label = (
        f"{turn_number}/{max_turns}"
        if max_turns not in (None, "")
        else f"{turn_number}/open"
    )
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
    turn_label = (
        f"{next_turn}/{max_turns}"
        if max_turns not in (None, "")
        else f"{next_turn}/open"
    )
    goal = str(
        session.get("goal") or session.get("topic") or "Advance the shared objective"
    ).strip()
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


async def _exchange_api_key_for_token(
    base_url: str, api_key: str, subject: str = "hermes"
) -> str:
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

    api_key = _a2a_session_store.get_remote_api_key(
        str(session.get("session_id") or "")
    )
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
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
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
        task_state=((response_json.get("result") or {}).get("status") or {}).get(
            "state"
        ),
    )

    return {
        "session_id": session.get("session_id"),
        "context_id": (response_json.get("result") or {}).get("context_id")
        or session.get("context_id"),
        "a2a_message_id": message_id,
        "duration_ms": duration_ms,
        "response": response_json,
    }


async def _run_session_turn(session_id: str) -> None:
    session = _a2a_session_store.get_session(session_id)
    if (
        not session
        or session.get("status") != "active"
        or session.get("next_action") != "compose_local_turn"
    ):
        return
    if not _hermes_client:
        _a2a_session_store.mark_error(
            session_id, "Direct Hermes client unavailable for managed A2A session"
        )
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
                _a2a_session_store.note_worker_claim(
                    session_id, delay_seconds=max(A2A_SESSION_TICK_SECONDS * 4, 6.0)
                )
                await _run_session_turn(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "Managed A2A session worker iteration failed",
                event="a2a.session.worker",
                error=str(exc),
            )
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


RADIUS_SKILLS_AUTO_UPDATE = _is_true(os.environ.get("RADIUS_SKILLS_AUTO_UPDATE", "false"))
RADIUS_SKILLS_REPO = os.environ.get("RADIUS_SKILLS_REPO", "radiustechsystems/skills")
RADIUS_SKILLS_BRANCH = str(os.environ.get("RADIUS_SKILLS_BRANCH", "main") or "").strip()
if RADIUS_SKILLS_BRANCH in {"*", "any"}:
    RADIUS_SKILLS_BRANCH = ""
RADIUS_SKILLS_WEBHOOK_SECRET = os.environ.get("RADIUS_SKILLS_WEBHOOK_SECRET", "")
RADIUS_SKILLS_GITHUB_TOKEN = os.environ.get("RADIUS_SKILLS_GITHUB_TOKEN", "")
RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS = int(os.environ.get("RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS", "90"))
RADIUS_SKILLS_STATE_PATH = Path(HERMES_HOME) / "external-skills" / ".radius-skills-state.json"
RADIUS_SKILLS_LOCK_PATH = Path(HERMES_HOME) / "external-skills" / ".radius-skills-lock"
RADIUS_SKILLS_STAGING_ROOT = Path(HERMES_HOME) / "external-skills" / ".radius-skills-staging"

_skills_sync_task: Optional[asyncio.Task] = None


def _read_config() -> dict[str, Any]:
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_git_branch_name(ref: str | None) -> str | None:
    value = str(ref or "").strip()
    if not value:
        return None
    if value.startswith("refs/heads/"):
        branch = value[len("refs/heads/") :].strip()
        return branch or None
    return value


def _expected_radius_skills_ref() -> str | None:
    if not RADIUS_SKILLS_BRANCH:
        return None
    return f"refs/heads/{RADIUS_SKILLS_BRANCH}"


def _default_radius_skills_state(last_error: Any = None) -> dict[str, Any]:
    return {
        "active_commit": None,
        "active_ref": None,
        "last_successful_sync_at": None,
        "last_completed_sync_at": None,
        "last_sync_started_at": None,
        "last_delivery_id": None,
        "last_seen_ref": None,
        "last_seen_before": None,
        "last_seen_after": None,
        "last_sync_result": None,
        "last_sync_trigger": None,
        "last_manifest_roots": [],
        "last_manifest_skill_count": 0,
        "last_published_skill_count": 0,
        "sync_in_progress": False,
        "last_error": last_error,
    }


def _load_radius_skills_state() -> dict[str, Any]:
    if not RADIUS_SKILLS_STATE_PATH.exists():
        return _default_radius_skills_state()
    try:
        loaded = json.loads(RADIUS_SKILLS_STATE_PATH.read_text(encoding="utf-8"))
        state = _default_radius_skills_state()
        if isinstance(loaded, dict):
            state.update(loaded)
        return state
    except Exception:
        return _default_radius_skills_state(last_error="state_unreadable")


def _save_radius_skills_state(patch: dict[str, Any]) -> dict[str, Any]:
    state = _load_radius_skills_state()
    state.update(patch)
    RADIUS_SKILLS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RADIUS_SKILLS_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def _parse_frontmatter(skill_md: str) -> dict[str, Any]:
    if not skill_md.startswith("---"):
        return {}
    end = skill_md.find("---", 3)
    if end < 0:
        return {}
    try:
        parsed = yaml.safe_load(skill_md[3:end]) or {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _scan_vendored_skills(source: Path | None = None) -> dict[str, Any]:
    source = source or Path(VENDORED_SKILLS_SOURCE)
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


def _validate_external_skills(source: Path) -> None:
    manifest = _scan_vendored_skills(source)
    for skill in manifest.get("skills", []):
        skill_md_path = Path(skill["path"]) / "SKILL.md"
        raw = skill_md_path.read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(raw)
        if not frontmatter.get("name") or not frontmatter.get("description"):
            raise ValueError(f"Invalid skill frontmatter in {skill_md_path}: required keys name and description")


def _write_vendored_manifest(source: Path) -> dict[str, Any]:
    manifest = _scan_vendored_skills(source)
    VENDORED_SKILLS_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    VENDORED_SKILLS_MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _refresh_well_known_skills(manifest: dict[str, Any]) -> None:
    well_known_root = Path(SKILLS_ROOT)
    staging_root = well_known_root.parent / f".well-known-skills-staging-{uuid.uuid4().hex[:8]}"
    backup_root = well_known_root.parent / f".well-known-skills-backup-{uuid.uuid4().hex[:8]}"
    staging_root.mkdir(parents=True, exist_ok=True)

    bundled = Path("/app/skills")
    if bundled.exists():
        for skill_file in sorted(bundled.glob("*.md")):
            if not skill_file.is_file():
                continue
            raw = skill_file.read_text(encoding="utf-8")
            if not _is_published(raw):
                continue
            skill_name = skill_file.stem
            target = staging_root / skill_name / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_file, target)

    for skill in manifest.get("skills", []):
        if not skill.get("published"):
            continue
        skill_name = skill["name"]
        source_skill = Path(skill["path"]) / "SKILL.md"
        target = staging_root / skill_name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_skill, target)

    if well_known_root.exists():
        well_known_root.rename(backup_root)
    staging_root.rename(well_known_root)
    if backup_root.exists():
        shutil.rmtree(backup_root, ignore_errors=True)


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
        well_known_dirs = sorted(
            p.name for p in well_known_root.iterdir() if p.is_dir()
        )
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
    empty = json.dumps(
        {
            "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
            "skills": [],
        },
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
            skills.append(
                {
                    "name": entry,
                    "type": "skill-md",
                    "description": _parse_description(content),
                    "url": f"{BASE_URL}/.well-known/agent-skills/{entry}/SKILL.md",
                    "digest": digest,
                }
            )
        except Exception:
            continue

    return json.dumps(
        {
            "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
            "skills": skills,
        },
        indent=2,
    )


def _get_index() -> str:
    global _skills_cache, _cache_built_at
    mock_index_file = " ".join(
        os.environ.get("MOCK_AGENT_SKILLS_INDEX_FILE", "").split()
    )
    if mock_index_file:
        try:
            return Path(mock_index_file).read_text(encoding="utf-8")
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "Mock skills index unavailable; falling back to generated index",
                event="skills.index.mock_unavailable",
                mock_index_file=mock_index_file,
                error=str(exc),
            )
    now = time.time()
    if not _skills_cache or now - _cache_built_at > _CACHE_TTL:
        _skills_cache = _build_index()
        _cache_built_at = now
    return _skills_cache


def _invalidate_skills_cache() -> None:
    global _skills_cache, _cache_built_at
    _skills_cache = None
    _cache_built_at = 0


def _git_auth_env() -> dict[str, str]:
    if not RADIUS_SKILLS_GITHUB_TOKEN:
        return {}
    auth_value = base64.b64encode(f"x-access-token:{RADIUS_SKILLS_GITHUB_TOKEN}".encode("utf-8")).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth_value}",
    }


def _sanitize_error_message(raw: str) -> str:
    return re.sub(r"https://[^:@/\s]+:[^@/\s]+@", "https://***:***@", raw or "")


def _run_git(cmd: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(_git_auth_env())
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=True, env=env)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or exc.__class__.__name__
        raise RuntimeError(_sanitize_error_message(detail)) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("git command timed out") from None


def _sync_radius_skills_repo(after_sha: str, branch_name: str) -> None:
    repo_dir = Path(RADIUS_SKILLS_DIR)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    RADIUS_SKILLS_STAGING_ROOT.mkdir(parents=True, exist_ok=True)

    clone_url = f"https://github.com/{RADIUS_SKILLS_REPO}.git"

    if not (repo_dir / ".git").exists():
        log_event(
            logger,
            logging.INFO,
            "Radius skills repo not present; cloning repository",
            event="skills.sync.repo.clone",
            repo=RADIUS_SKILLS_REPO,
            branch=branch_name,
            after=after_sha,
            repo_dir=str(repo_dir),
        )
        staging = RADIUS_SKILLS_STAGING_ROOT / f"clone-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        _run_git(["git", "clone", "--branch", branch_name, "--single-branch", clone_url, str(staging)], RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS)
        _run_git(["git", "-C", str(staging), "checkout", after_sha], RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS)
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        shutil.move(str(staging), str(repo_dir))
        return

    log_event(
        logger,
        logging.INFO,
        "Updating managed Radius skills repository",
        event="skills.sync.repo.update",
        repo=RADIUS_SKILLS_REPO,
        branch=branch_name,
        after=after_sha,
        repo_dir=str(repo_dir),
    )
    _run_git(
        [
            "git",
            "-C",
            str(repo_dir),
            "fetch",
            "origin",
            f"refs/heads/{branch_name}:refs/remotes/origin/{branch_name}",
        ],
        RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS,
    )
    _run_git(
        [
            "git",
            "-C",
            str(repo_dir),
            "checkout",
            "-B",
            branch_name,
            f"refs/remotes/origin/{branch_name}",
        ],
        RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS,
    )
    _run_git(["git", "-C", str(repo_dir), "reset", "--hard", after_sha], RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS)


def _sync_radius_skills_stateful(
    after_sha: str,
    before_sha: str | None,
    delivery_id: str | None,
    ref: str | None = None,
) -> dict[str, Any]:
    RADIUS_SKILLS_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RADIUS_SKILLS_LOCK_PATH.open("a+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        branch_name = _normalize_git_branch_name(ref) or RADIUS_SKILLS_BRANCH or "main"

        state = _load_radius_skills_state()
        if state.get("active_commit") == after_sha and state.get("active_ref") == ref:
            return _save_radius_skills_state(
                {
                    "active_ref": ref,
                    "last_delivery_id": delivery_id,
                    "last_seen_ref": ref,
                    "last_seen_before": before_sha,
                    "last_seen_after": after_sha,
                    "last_completed_sync_at": _now_iso(),
                    "last_sync_result": "already_current",
                    "last_sync_trigger": "github_webhook" if delivery_id and delivery_id != "manual" else "manual",
                    "sync_in_progress": False,
                }
            )

        _save_radius_skills_state(
            {
                "last_sync_started_at": _now_iso(),
                "sync_in_progress": True,
                "last_error": None,
                "last_delivery_id": delivery_id,
                "last_seen_ref": ref,
                "last_seen_before": before_sha,
                "last_seen_after": after_sha,
                "last_sync_result": "in_progress",
                "last_sync_trigger": "github_webhook" if delivery_id and delivery_id != "manual" else "manual",
            }
        )

        try:
            log_event(
                logger,
                logging.INFO,
                "Radius skills sync started",
                event="skills.sync.started",
                repo=RADIUS_SKILLS_REPO,
                branch=branch_name,
                ref=ref,
                before=before_sha,
                after=after_sha,
                delivery_id=delivery_id,
            )
            _sync_radius_skills_repo(after_sha, branch_name)
            log_event(
                logger,
                logging.INFO,
                "Radius skills repository updated; validating skill metadata",
                event="skills.sync.validating",
                repo=RADIUS_SKILLS_REPO,
                branch=branch_name,
                ref=ref,
                after=after_sha,
                skills_dir=RADIUS_SKILLS_DIR,
            )
            _validate_external_skills(Path(RADIUS_SKILLS_DIR))
            manifest = _write_vendored_manifest(Path(RADIUS_SKILLS_DIR))
            published_skills = sum(
                1 for skill in manifest.get("skills", []) if skill.get("published")
            )
            log_event(
                logger,
                logging.INFO,
                "Radius skills manifest written",
                event="skills.sync.manifest",
                repo=RADIUS_SKILLS_REPO,
                branch=branch_name,
                ref=ref,
                after=after_sha,
                skill_count=len(manifest.get("skills", [])),
                published_skill_count=published_skills,
                roots=manifest.get("roots", []),
                manifest_path=str(VENDORED_SKILLS_MANIFEST),
            )
            _refresh_well_known_skills(manifest)
            _invalidate_skills_cache()
            return _save_radius_skills_state(
                {
                    "active_commit": after_sha,
                    "active_ref": ref,
                    "last_successful_sync_at": _now_iso(),
                    "last_completed_sync_at": _now_iso(),
                    "last_sync_result": "success",
                    "last_manifest_roots": manifest.get("roots", []),
                    "last_manifest_skill_count": len(manifest.get("skills", [])),
                    "last_published_skill_count": published_skills,
                    "sync_in_progress": False,
                    "last_error": None,
                }
            )
        except Exception as exc:
            error_message = _sanitize_error_message(str(exc))
            return _save_radius_skills_state(
                {
                    "last_completed_sync_at": _now_iso(),
                    "sync_in_progress": False,
                    "last_sync_result": "error",
                    "last_error": error_message,
                }
            )
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


async def _run_radius_sync_task(
    after_sha: str,
    before_sha: str | None = None,
    delivery_id: str | None = None,
    ref: str | None = None,
) -> None:
    result = await asyncio.to_thread(
        _sync_radius_skills_stateful, after_sha, before_sha, delivery_id, ref
    )
    if result.get("last_error"):
        log_event(
            logger,
            logging.ERROR,
            "Radius skills sync failed",
            event="skills.sync",
            repo=RADIUS_SKILLS_REPO,
            branch=_normalize_git_branch_name(result.get("last_seen_ref")) or RADIUS_SKILLS_BRANCH or "main",
            ref=result.get("last_seen_ref"),
            before=before_sha,
            after=after_sha,
            delivery_id=delivery_id,
            result=result.get("last_sync_result"),
            error=result.get("last_error"),
        )
    else:
        log_event(
            logger,
            logging.INFO,
            "Radius skills sync completed",
            event="skills.sync",
            repo=RADIUS_SKILLS_REPO,
            branch=_normalize_git_branch_name(result.get("last_seen_ref")) or RADIUS_SKILLS_BRANCH or "main",
            ref=result.get("last_seen_ref"),
            before=before_sha,
            after=after_sha,
            delivery_id=delivery_id,
            result=result.get("last_sync_result"),
            active_commit=result.get("active_commit"),
            skill_count=result.get("last_manifest_skill_count"),
            published_skill_count=result.get("last_published_skill_count"),
        )


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
    _a2a_session_worker = asyncio.create_task(
        _session_worker_loop(), name="a2a-session-worker"
    )
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
        vendored_skill_names=[
            skill.get("name") for skill in vendored_manifest.get("skills", [])
        ],
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
        return apply_browser_security_headers(
            Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
            },
            ),
            request.url.path,
        )
    if request.url.path.startswith("/.well-known/agent-skills/"):
        if request.method == "OPTIONS":
            return apply_browser_security_headers(
                Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type",
                    },
                ),
                request.url.path,
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return apply_browser_security_headers(response, request.url.path)
    response = await call_next(request)
    return apply_browser_security_headers(response, request.url.path)


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
        return apply_browser_security_headers(response, request.url.path)
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
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "public, max-age=60",
    }
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
        headers = {
            "Cache-Control": "public, max-age=300",
            "Content-Length": str(len(raw)),
        }
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers)
        return Response(
            content=raw, media_type="text/markdown; charset=utf-8", headers=headers
        )
    except Exception as e:
        log_event(
            logger,
            logging.ERROR,
            "Failed reading skill file",
            event="skills.read_error",
            skill_name=name,
            skill_path=str(skill_path),
            error=str(e),
        )
        return PlainTextResponse("Internal Server Error", status_code=500)


@app.get("/debug/skills")
async def debug_skills(auth: dict = Depends(jwt_auth_dep)):
    if not _is_true(os.environ.get("DEBUG_SKILLS")):
        return PlainTextResponse("Not Found", status_code=404)
    return JSONResponse(_debug_skills_payload(), headers={"Cache-Control": "no-store"})


@app.get("/internal/skills/status")
async def internal_skills_status(_: None = Depends(_internal_auth_dep)):
    state = _load_radius_skills_state()
    redacted_error = _sanitize_error_message(str(state.get("last_error") or "")) or None
    return JSONResponse(
        {
            "enabled": RADIUS_SKILLS_AUTO_UPDATE,
            "repo": RADIUS_SKILLS_REPO,
            "branch": RADIUS_SKILLS_BRANCH or None,
            "skills_dir": RADIUS_SKILLS_DIR,
            "active_commit": state.get("active_commit"),
            "active_ref": state.get("active_ref"),
            "last_successful_sync_at": state.get("last_successful_sync_at"),
            "last_completed_sync_at": state.get("last_completed_sync_at"),
            "last_sync_started_at": state.get("last_sync_started_at"),
            "last_delivery_id": state.get("last_delivery_id"),
            "last_seen_ref": state.get("last_seen_ref"),
            "last_seen_before": state.get("last_seen_before"),
            "last_seen_after": state.get("last_seen_after"),
            "last_sync_result": state.get("last_sync_result"),
            "last_sync_trigger": state.get("last_sync_trigger"),
            "last_manifest_roots": state.get("last_manifest_roots") or [],
            "last_manifest_skill_count": int(state.get("last_manifest_skill_count") or 0),
            "last_published_skill_count": int(state.get("last_published_skill_count") or 0),
            "sync_in_progress": bool(state.get("sync_in_progress")),
            "last_error": redacted_error,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/internal/skills/sync")
async def internal_skills_sync(request: Request, _: None = Depends(_internal_auth_dep)):
    if not RADIUS_SKILLS_AUTO_UPDATE:
        return JSONResponse({"ok": False, "error": "disabled"}, status_code=409)
    body = await request.json() if request.headers.get("Content-Type", "").startswith("application/json") else {}
    target_sha = str((body or {}).get("after") or "").strip()
    if not target_sha:
        try:
            result = await asyncio.to_thread(
                _run_git,
                ["git", "-C", RADIUS_SKILLS_DIR, "rev-parse", "HEAD"],
                RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS,
            )
            target_sha = result.stdout.strip()
        except Exception:
            return JSONResponse({"ok": False, "error": "target_sha_required"}, status_code=400)
    asyncio.create_task(_run_radius_sync_task(target_sha, delivery_id="manual"))
    return JSONResponse({"ok": True, "queued": True, "after": target_sha}, status_code=202)


@app.post("/webhooks/github/radius-skills")
async def radius_skills_webhook(request: Request):
    global _skills_sync_task
    if not RADIUS_SKILLS_AUTO_UPDATE:
        log_event(
            logger,
            logging.WARNING,
            "Radius skills webhook ignored because auto-update is disabled",
            event="skills.webhook",
            result="disabled",
        )
        return JSONResponse({"ok": False, "error": "disabled"}, status_code=404)
    if not RADIUS_SKILLS_WEBHOOK_SECRET:
        log_event(
            logger,
            logging.ERROR,
            "Radius skills webhook rejected because no secret is configured",
            event="skills.webhook",
            result="secret_not_configured",
        )
        return JSONResponse({"ok": False, "error": "secret_not_configured"}, status_code=503)
    event_name = request.headers.get("X-GitHub-Event")
    delivery_id = request.headers.get("X-GitHub-Delivery")
    if event_name != "push":
        log_event(
            logger,
            logging.INFO,
            "Radius skills webhook ignored because event is unsupported",
            event="skills.webhook",
            result="unsupported_event",
            github_event=event_name,
            delivery_id=delivery_id,
        )
        return JSONResponse({"ok": False, "error": "unsupported_event"}, status_code=202)

    payload_bytes = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        RADIUS_SKILLS_WEBHOOK_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        log_event(
            logger,
            logging.WARNING,
            "Radius skills webhook rejected because signature verification failed",
            event="skills.webhook",
            result="invalid_signature",
            github_event=event_name,
            delivery_id=delivery_id,
        )
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        log_event(
            logger,
            logging.WARNING,
            "Radius skills webhook rejected because payload was invalid JSON",
            event="skills.webhook",
            result="invalid_json",
            github_event=event_name,
            delivery_id=delivery_id,
        )
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    repo_name = ((payload.get("repository") or {}).get("full_name") or "").strip()
    ref = str(payload.get("ref") or "").strip()
    before = str(payload.get("before") or "").strip() or None
    after = str(payload.get("after") or "").strip()
    expected_ref = _expected_radius_skills_ref()
    is_branch_push = ref.startswith("refs/heads/")

    if repo_name != RADIUS_SKILLS_REPO or not is_branch_push or (
        expected_ref is not None and ref != expected_ref
    ):
        log_event(
            logger,
            logging.INFO,
            "Radius skills webhook ignored because repository or branch did not match",
            event="skills.webhook",
            result="ignored",
            github_event=event_name,
            delivery_id=delivery_id,
            repo=repo_name,
            ref=ref,
            expected_repo=RADIUS_SKILLS_REPO,
            expected_ref=expected_ref,
        )
        return JSONResponse({"ok": True, "ignored": True}, status_code=202)
    if not after:
        log_event(
            logger,
            logging.WARNING,
            "Radius skills webhook rejected because after SHA was missing",
            event="skills.webhook",
            result="missing_after_sha",
            github_event=event_name,
            delivery_id=delivery_id,
            repo=repo_name,
            ref=ref,
        )
        return JSONResponse({"ok": False, "error": "missing_after_sha"}, status_code=400)

    _save_radius_skills_state(
        {
            "last_delivery_id": delivery_id,
            "last_seen_ref": ref,
            "last_seen_before": before,
            "last_seen_after": after,
            "last_sync_trigger": "github_webhook",
        }
    )
    log_event(
        logger,
        logging.INFO,
        "Radius skills webhook accepted and sync queued",
        event="skills.webhook",
        result="queued",
        github_event=event_name,
        delivery_id=delivery_id,
        repo=repo_name,
        ref=ref,
        before=before,
        after=after,
    )
    _skills_sync_task = asyncio.create_task(
        _run_radius_sync_task(after, before, delivery_id, ref)
    )
    return JSONResponse(
        {
            "ok": True,
            "queued": True,
            "repo": repo_name,
            "ref": ref,
            "branch_mode": "any" if expected_ref is None else "pinned",
            "before": before,
            "after": after,
            "delivery_id": delivery_id,
            "status_path": "/internal/skills/status",
        },
        status_code=202,
    )


@app.post("/internal/a2a/sessions/outbound")
async def internal_a2a_session_outbound(
    request: Request, _: None = Depends(_internal_auth_dep)
):
    payload = await request.json()
    try:
        session = _a2a_session_store.create_or_update_outbound(
            payload if isinstance(payload, dict) else {}
        )
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
    return JSONResponse(
        {"ok": True, "session": _a2a_session_store.serialize_for_response(session)}
    )


@app.post("/internal/a2a/sessions/outbound-result")
async def internal_a2a_session_outbound_result(
    request: Request, _: None = Depends(_internal_auth_dep)
):
    payload = await request.json()
    try:
        session = _a2a_session_store.record_outbound_result(
            payload if isinstance(payload, dict) else {}
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not session:
        return JSONResponse(
            {"ok": False, "error": "session_not_found"}, status_code=404
        )
    return JSONResponse(
        {"ok": True, "session": _a2a_session_store.serialize_for_response(session)}
    )


@app.post("/internal/a2a/sessions/resolve")
async def internal_a2a_session_resolve(
    request: Request, _: None = Depends(_internal_auth_dep)
):
    payload = await request.json()
    payload = payload if isinstance(payload, dict) else {}
    session = _a2a_session_store.find_active_session(
        remote_agent=payload.get("remote_agent"),
        context_id=payload.get("context_id"),
        origin_platform=(
            (payload.get("origin") or {}).get("platform")
            if isinstance(payload.get("origin"), dict)
            else None
        ),
        origin_chat_id=(
            (payload.get("origin") or {}).get("chat_id")
            if isinstance(payload.get("origin"), dict)
            else None
        ),
    )
    return JSONResponse(
        {"ok": True, "session": _a2a_session_store.serialize_for_response(session)}
    )


@app.get("/internal/a2a/sessions/{session_id}")
async def internal_a2a_session_get(
    session_id: str, _: None = Depends(_internal_auth_dep)
):
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
    return Response(
        content=json.dumps(doc),
        media_type="application/did+json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=600",
        },
    )


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
    return JSONResponse(
        registration,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=60",
        },
    )


@app.get("/.well-known/agent-card.json")
async def agent_card():
    return JSONResponse(
        _build_agent_card_payload(),
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=60",
        },
    )


def _build_agent_card_payload() -> dict:
    agent_name = os.environ.get("AGENT_NAME", "Hermes Agent")
    did = get_did()
    webhook_enabled = bool(os.environ.get("WEBHOOK_SECRET"))
    skills_index = json.loads(_get_index())
    mode = A2A_MODE
    direct = _direct_available()
    streaming = mode == "direct" or (mode == "auto" and direct)
    skills = [
        {
            "id": s["name"],
            "name": s["name"],
            "description": s.get("description", ""),
            "tags": ["a2a-direct", "a2a-delegated"],
            "input_modes": ["text/plain"],
            "output_modes": ["text/plain"],
        }
        for s in skills_index.get("skills", [])
    ]
    return {
        "name": agent_name,
        "description": os.environ.get(
            "AGENT_DESCRIPTION",
            f"Name: {agent_name}",
        ),
        "version": "1.1.0",
        "provider": {
            "name": agent_name,
            "url": BASE_URL,
            **({"did": did} if did else {}),
        },
        "supported_interfaces": [
            {
                "protocol_binding": "JSONRPC",
                "url": f"{BASE_URL}/a2a",
                "protocol_version": "1.0",
            }
        ],
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


def _humanize_slug(value: str) -> str:
    acronyms = {
        "a2a": "A2A",
        "did": "DID",
        "jwt": "JWT",
        "erc": "ERC",
        "rusd": "RUSD",
        "sbc": "SBC",
    }
    words = re.split(r"[-_\s]+", (value or "").strip())
    return (
        " ".join(
            acronyms.get(word.lower(), word.capitalize()) for word in words if word
        )
        or "Unknown"
    )


def _truncate_text(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _skill_badges(name: str, description: str) -> list[str]:
    haystack = f"{name} {description}".lower()
    badges: list[str] = []
    if "wallet" in haystack or "token" in haystack or "balance" in haystack:
        badges.append("Wallet")
    if "a2a" in haystack or "agent-to-agent" in haystack or "jwt" in haystack:
        badges.append("A2A")
    if "erc-8004" in haystack or "registration" in haystack or "register" in haystack:
        badges.append("Registration")
    if not badges:
        badges.append("Capability")
    return badges[:2]


def _format_skill_cards(published_skills: list[dict]) -> str:
    cards: list[str] = []
    for skill in published_skills:
        name = skill.get("name", "unknown")
        description = skill.get("description") or "Published capability"
        badges_html = "".join(
            f"<span class='mini-pill'>{html.escape(badge)}</span>"
            for badge in _skill_badges(name, description)
        )
        cards.append(
            "<article class='skill-card'>"
            f"<div class='skill-card-top'><h3>{html.escape(_humanize_slug(name))}</h3><div class='mini-pills'>{badges_html}</div></div>"
            f"<p>{html.escape(_truncate_text(description, 150))}</p>"
            f"<div class='skill-slug'>/{html.escape(name)}</div>"
            "</article>"
        )
    if not cards:
        cards.append(
            "<article class='skill-card empty'>"
            "<div class='skill-card-top'><h3>No Published Skills Yet</h3></div>"
            "<p>This agent has not exposed public skills yet, but the discovery documents below are still live.</p>"
            "</article>"
        )
    return "".join(cards)


def _format_discovery_cards(agent_card: dict) -> tuple[str, str]:
    interface_url = (
        agent_card.get("supported_interfaces", [{}])[0].get("url") or f"{BASE_URL}/a2a"
    )
    discovery_links = [
        (
            "Agent Card",
            "/.well-known/agent-card.json",
            "A2A interface, auth scheme, and advertised capabilities.",
        ),
        (
            "Skills Index",
            "/.well-known/agent-skills/index.json",
            "Published capability surface for agent browsers and tooling.",
        ),
        (
            "DID Document",
            "/.well-known/did.json",
            "Public signing identity for DID-based trust and verification.",
        ),
        (
            "ERC-8004 Registration",
            "/.well-known/agent-registration.json",
            "Registration profile linking this agent to onchain identity.",
        ),
    ]
    cards = []
    for label, href, note in discovery_links:
        cards.append(
            "<a class='discovery-card' href='{href}'>"
            "<span>{label}</span>"
            "<strong>{uri}</strong>"
            "<p>{note}</p>"
            "</a>".format(
                href=html.escape(href),
                label=html.escape(label),
                uri=html.escape(href),
                note=html.escape(note),
            )
        )
    interface_bits = [
        ("A2A Endpoint", interface_url),
        ("Auth", "Bearer JWT"),
        (
            "Streaming",
            "Enabled"
            if agent_card.get("capabilities", {}).get("streaming")
            else "Standard requests",
        ),
        (
            "Modes",
            ", ".join(agent_card.get("capabilities", {}).get("a2a_modes", []))
            or "Unavailable",
        ),
    ]
    facts_html = "".join(
        "<div class='fact-row'><span>{label}</span><strong>{value}</strong></div>".format(
            label=html.escape(label),
            value=html.escape(value),
        )
        for label, value in interface_bits
    )
    return "".join(cards), facts_html


def _enabled_toolsets() -> set[str]:
    raw = (_read_config().get("toolsets") or [])
    return {str(item).strip() for item in raw if str(item).strip()}


def _token_exchange_enabled() -> bool:
    return bool(os.environ.get("JWT_EXCHANGE_KEY") or os.environ.get("JWT_API_KEY"))


def _scan_bundled_skills() -> dict[str, dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    roots = [Path(HERMES_HOME) / "skills", REPO_ROOT / "skills"]
    for root in roots:
        if not root.exists():
            continue
        for skill_file in sorted(root.glob("*.md")):
            try:
                raw = skill_file.read_text(encoding="utf-8")
            except Exception:
                raw = ""
            discovered.setdefault(
                skill_file.stem,
                {
                    "name": skill_file.stem,
                    "description": _parse_description(raw),
                    "published": _is_published(raw),
                    "origin": "bundled",
                },
            )

        radius_root = root / "radius"
        if not radius_root.exists():
            continue
        for skill_md in sorted(radius_root.glob("*/SKILL.md")):
            skill_name = skill_md.parent.name
            try:
                raw = skill_md.read_text(encoding="utf-8")
            except Exception:
                raw = ""
            discovered.setdefault(
                skill_name,
                {
                    "name": skill_name,
                    "description": _parse_description(raw),
                    "published": _is_published(raw),
                    "origin": "bundled-radius",
                },
            )
    return discovered


def _load_plugin_manifests() -> list[dict[str, Any]]:
    plugin_roots = [Path(HERMES_HOME) / "plugins", REPO_ROOT / "plugins"]
    plugins_by_name: dict[str, dict[str, Any]] = {}
    for root in plugin_roots:
        if not root.exists():
            continue
        for manifest_path in sorted(root.glob("*/plugin.yaml")):
            try:
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            name = str(manifest.get("name") or manifest_path.parent.name).strip()
            if not name or name in plugins_by_name:
                continue
            tools = [
                str(tool).strip()
                for tool in (manifest.get("provides_tools") or [])
                if str(tool).strip()
            ]
            plugins_by_name[name] = {
                "name": name,
                "description": str(manifest.get("description") or "").strip(),
                "tools": tools,
                "path": str(manifest_path.parent),
            }
    return [plugins_by_name[name] for name in sorted(plugins_by_name)]


def _build_agent_graph_payload() -> dict[str, Any]:
    agent_card = _build_agent_card_payload()
    published_index = json.loads(_get_index())
    published_by_name = {
        str(skill.get("name") or ""): skill
        for skill in (published_index.get("skills") or [])
        if str(skill.get("name") or "").strip()
    }
    bundled_skills = _scan_bundled_skills()
    vendored_manifest = _load_vendored_manifest()
    if not vendored_manifest.get("skills"):
        vendored_manifest = _scan_vendored_skills()
    plugins = _load_plugin_manifests()
    enabled_toolsets = _enabled_toolsets()
    all_toolsets_enabled = "all" in enabled_toolsets

    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()

    def add_node(
        node_id: str,
        label: str,
        *,
        kind: str,
        category: str,
        external: bool,
        detail: str,
        href: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "id": node_id,
            "label": label,
            "kind": kind,
            "category": category,
            "external": external,
            "detail": detail,
            "href": href,
            "status": status,
            "tags": sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()}),
        }
        if metadata:
            record["metadata"] = metadata
        nodes[node_id] = record

    def add_edge(source: str, target: str, kind: str) -> None:
        edges.add((source, target, kind))

    agent_name = agent_card["name"]
    add_node(
        "agent",
        agent_name,
        kind="agent",
        category="core",
        external=False,
        detail=agent_card.get("description") or f"{agent_name} capability map",
        href=f"{BASE_URL}/",
        status="active",
        tags=["core"],
    )

    capability_ids: dict[str, str] = {}

    def add_capability(
        slug: str,
        label: str,
        detail: str,
        *,
        status: str = "available",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        node_id = f"capability:{slug}"
        capability_ids[slug] = node_id
        add_node(
            node_id,
            label,
            kind="capability",
            category="capabilities",
            external=False,
            detail=detail,
            status=status,
            tags=tags,
            metadata=metadata,
        )
        add_edge("agent", node_id, "contains")
        return node_id

    a2a_modes = agent_card.get("capabilities", {}).get("a2a_modes", [])
    add_capability(
        "a2a",
        "A2A Transport",
        (
            f"JSON-RPC over HTTP at /a2a with modes: {', '.join(a2a_modes) or 'unavailable'}."
        ),
        status="enabled",
        tags=["A2A", "JSON-RPC"],
        metadata={"source": "runtime", "tools": [], "interfaces": [f"{BASE_URL}/a2a"]},
    )
    add_capability(
        "jwt",
        "JWT Auth",
        "Bearer JWT auth backed by DID-signed ES256K tokens and optional API-key exchange.",
        status="enabled",
        tags=["JWT", "DID"],
        metadata={"source": "runtime", "tools": []},
    )
    add_capability(
        "wallet",
        "Radius Wallet",
        (
            "Built-in Radius wallet and balances are surfaced on the public homepage."
            if _wallet_address()
            else "Wallet capability is wired, but no public wallet address is currently available."
        ),
        status="enabled" if _wallet_address() else "configured",
        tags=["Radius", "Wallet"],
        metadata={"source": "runtime", "tools": []},
    )
    add_capability(
        "erc8004",
        "ERC-8004",
        "Registration payloads are exposed publicly and backed by deterministic registry tools.",
        status="enabled",
        tags=["ERC-8004", "Registration"],
        metadata={"source": "runtime", "tools": []},
    )
    if os.environ.get("WEBHOOK_SECRET"):
        add_capability(
            "push",
            "Push Notifications",
            "Delegated A2A flows can use webhook-backed push notifications.",
            status="enabled",
            tags=["Webhook", "A2A"],
            metadata={"source": "runtime", "tools": []},
        )
    if os.environ.get("BYTEROVER_API_KEY") or _is_true(os.environ.get("BYTEROVER_LOCAL")):
        add_capability(
            "memory",
            "ByteRover Memory",
            "Structured long-term memory is configured for durable project and wallet context.",
            status="enabled",
            tags=["Memory"],
            metadata={"source": "runtime", "tools": []},
        )
    if os.environ.get("PARA_API_KEY") or os.environ.get("PARA_WALLET_ID"):
        add_capability(
            "para",
            "Para Wallet",
            "Optional Para-backed session wallet provider is configured alongside the local Radius wallet.",
            status="enabled",
            tags=["Wallet", "Para"],
            metadata={"source": "runtime", "tools": []},
        )

    surface_specs = [
        (
            "surface:agent-card",
            "Agent Card",
            "surfaces",
            f"{BASE_URL}/.well-known/agent-card.json",
            "A2A identity, auth scheme, and advertised skills.",
        ),
        (
            "surface:skills-index",
            "Skills Index",
            "surfaces",
            f"{BASE_URL}/.well-known/agent-skills/index.json",
            "Published capability surface for discovery clients and browsers.",
        ),
        (
            "surface:did",
            "DID Document",
            "surfaces",
            f"{BASE_URL}/.well-known/did.json",
            "Public DID used to verify bearer JWT signatures.",
        ),
        (
            "surface:registration",
            "ERC-8004 Registration",
            "surfaces",
            f"{BASE_URL}/.well-known/agent-registration.json",
            "Onchain-compatible registration profile published from the current runtime.",
        ),
        (
            "surface:api-catalog",
            "API Catalog",
            "surfaces",
            f"{BASE_URL}/.well-known/api-catalog",
            "Linkset that ties the public docs and A2A service descriptors together.",
        ),
        (
            "surface:a2a",
            "A2A Endpoint",
            "interfaces",
            f"{BASE_URL}/a2a",
            "JSON-RPC request endpoint for agent-to-agent calls.",
        ),
        (
            "surface:openapi",
            "OpenAPI Spec",
            "interfaces",
            f"{BASE_URL}/openapi.json",
            "Machine-readable OpenAPI descriptor for HTTP clients and API catalogs.",
        ),
        (
            "surface:health",
            "Health Endpoint",
            "interfaces",
            f"{BASE_URL}/health",
            "Operational status endpoint referenced by automated discovery clients.",
        ),
    ]
    if _token_exchange_enabled():
        surface_specs.append(
            (
                "surface:token",
                "Token Exchange",
                "interfaces",
                f"{BASE_URL}/token",
                "Short-lived bearer-token exchange endpoint when an API key is configured.",
            )
        )

    for node_id, label, category, href, detail in surface_specs:
        add_node(
            node_id,
            label,
            kind="surface",
            category=category,
            external=True,
            detail=detail,
            href=href,
            status="public",
            tags=["public"],
            metadata={"surface_type": category},
        )
        add_edge("agent", node_id, "exposes")

    add_edge("surface:agent-card", capability_ids["a2a"], "describes")
    add_edge("surface:a2a", capability_ids["a2a"], "invokes")
    add_edge("surface:did", capability_ids["jwt"], "verifies")
    add_edge("surface:registration", capability_ids["erc8004"], "describes")
    if "surface:token" in nodes:
        add_edge("surface:token", capability_ids["jwt"], "issues")

    channel_specs = [
        ("telegram", "Telegram", bool(os.environ.get("TELEGRAM_BOT_TOKEN")), "Bot token configured."),
        ("discord", "Discord", bool(os.environ.get("DISCORD_BOT_TOKEN")), "Bot token configured."),
        (
            "slack",
            "Slack",
            bool(os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_APP_TOKEN")),
            "Bot and app tokens configured.",
        ),
        (
            "whatsapp",
            "WhatsApp",
            _is_true(os.environ.get("WHATSAPP_ENABLED")),
            "WhatsApp transport is enabled for the gateway.",
        ),
    ]
    for slug, label, enabled, detail in channel_specs:
        if not enabled:
            continue
        node_id = f"channel:{slug}"
        add_node(
            node_id,
            label,
            kind="channel",
            category="channels",
            external=False,
            detail=detail,
            status="enabled",
            tags=["Messaging"],
        )
        add_edge("agent", node_id, "routes")

    skill_catalog: dict[str, dict[str, Any]] = {}
    for name, skill in bundled_skills.items():
        skill_catalog[name] = {
            "name": name,
            "description": skill.get("description") or "Bundled Hermes skill",
            "origins": [skill.get("origin") or "bundled"],
            "published": bool(skill.get("published")),
            "href": f"{BASE_URL}/.well-known/agent-skills/{name}/SKILL.md"
            if skill.get("published")
            else None,
        }
    for skill in vendored_manifest.get("skills", []):
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        record = skill_catalog.setdefault(
            name,
            {
                "name": name,
                "description": str(skill.get("description") or "Vendored Radius skill").strip(),
                "origins": [],
                "published": False,
                "href": None,
            },
        )
        record["description"] = record["description"] or str(skill.get("description") or "").strip()
        record["origins"] = sorted({*record["origins"], "vendored"})
        if skill.get("published"):
            record["published"] = True
            record["href"] = f"{BASE_URL}/.well-known/agent-skills/{name}/SKILL.md"
    for name, skill in published_by_name.items():
        record = skill_catalog.setdefault(
            name,
            {
                "name": name,
                "description": str(skill.get("description") or "Published capability").strip(),
                "origins": [],
                "published": False,
                "href": None,
            },
        )
        record["description"] = str(skill.get("description") or record["description"]).strip()
        record["published"] = True
        record["href"] = str(skill.get("url") or record["href"] or "").strip() or None
        record["origins"] = sorted({*record["origins"], "published"})

    for name in sorted(skill_catalog):
        skill = skill_catalog[name]
        node_id = f"skill:{name}"
        status = "published" if skill.get("published") else "internal"
        add_node(
            node_id,
            _humanize_slug(name),
            kind="skill",
            category="skills",
            external=False,
            detail=skill.get("description") or "Installed skill",
            href=skill.get("href"),
            status=status,
            tags=[*skill.get("origins", []), status],
        )
        add_edge("agent", node_id, "contains")
        if skill.get("published"):
            add_edge("surface:skills-index", node_id, "lists")
            add_edge("surface:agent-card", node_id, "advertises")

    plugin_capability_targets = {
        "radius-cast": "wallet",
        "gen-jwt": "jwt",
        "erc8004-registry": "erc8004",
        "a2a-send": "a2a",
        "agent-info": "a2a",
    }

    for plugin in plugins:
        plugin_name = plugin["name"]
        enabled = all_toolsets_enabled or plugin_name in enabled_toolsets
        tools = [str(tool).strip() for tool in plugin.get("tools", []) if str(tool).strip()]
        target_slug = plugin_capability_targets.get(plugin_name)
        if target_slug and target_slug in capability_ids:
            capability_id = capability_ids[target_slug]
            capability = nodes[capability_id]
            metadata = dict(capability.get("metadata") or {})
            metadata["source"] = metadata.get("source") or "runtime"
            metadata["plugins"] = sorted({*(metadata.get("plugins") or []), plugin_name})
            metadata["tools"] = sorted({*(metadata.get("tools") or []), *tools})
            capability["metadata"] = metadata
            capability["tags"] = sorted({*(capability.get("tags") or []), "plugin-backed"})
            capability_id_for_tools = capability_id
        else:
            capability_id_for_tools = add_capability(
                f"toolset-{plugin_name}",
                _humanize_slug(plugin_name),
                plugin.get("description") or "Bundled capability",
                status="enabled" if enabled else "bundled",
                tags=["plugin-backed", "enabled" if enabled else "bundled"],
                metadata={
                    "source": "plugin",
                    "plugins": [plugin_name],
                    "tools": tools,
                    "path": plugin.get("path"),
                },
            )

        for tool_name in tools:
            tool_id = f"tool:{tool_name}"
            add_node(
                tool_id,
                _humanize_slug(tool_name),
                kind="tool",
                category="capabilities",
                external=False,
                detail=f"{tool_name} is provided by the {plugin_name} plugin.",
                status="enabled" if enabled else "bundled",
                tags=[plugin_name],
            )
            add_edge(capability_id_for_tools, tool_id, "provides")

    serialized_edges = [
        {"source": source, "target": target, "kind": kind}
        for source, target, kind in sorted(edges)
        if source in nodes and target in nodes
    ]
    serialized_nodes = [nodes[node_id] for node_id in sorted(nodes)]
    external_count = sum(1 for node in serialized_nodes if node.get("external"))
    capability_count = sum(1 for node in serialized_nodes if node.get("kind") == "capability")
    tool_count = sum(1 for node in serialized_nodes if node.get("kind") == "tool")

    return {
        "generated_at": _now_iso(),
        "agent": {
            "id": "agent",
            "label": agent_name,
            "description": agent_card.get("description") or "",
        },
        "stats": {
            "node_count": len(serialized_nodes),
            "edge_count": len(serialized_edges),
            "external_count": external_count,
            "internal_count": len(serialized_nodes) - external_count,
            "published_skill_count": len(published_by_name),
            "capability_count": capability_count,
            "plugin_count": len(plugins),
            "tool_count": tool_count,
        },
        "nodes": serialized_nodes,
        "edges": serialized_edges,
    }


def _format_graph_fallback(graph_payload: dict[str, Any]) -> str:
    group_labels = {
        "surfaces": "External Surfaces",
        "interfaces": "External Interfaces",
        "capabilities": "Capabilities",
        "skills": "Skills",
        "tools": "Tools",
        "channels": "Channels",
    }
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in group_labels}
    for node in graph_payload.get("nodes", []):
        if node.get("kind") == "agent":
            continue
        category = str(node.get("category") or "capabilities")
        grouped.setdefault(category, []).append(node)

    cards: list[str] = []
    for category, label in group_labels.items():
        entries = sorted(grouped.get(category, []), key=lambda item: str(item.get("label") or ""))
        if not entries:
            continue
        items: list[str] = []
        for entry in entries:
            status = str(entry.get("status") or "").strip()
            detail = str(entry.get("detail") or "").strip()
            href = str(entry.get("href") or "").strip()
            title = html.escape(str(entry.get("label") or "Unknown"))
            meta = (
                f"<a href='{html.escape(href)}' target='_blank' rel='noopener'>{title}</a>"
                if href
                else title
            )
            badge = (
                f"<span class='graph-fallback-badge'>{html.escape(status)}</span>"
                if status
                else ""
            )
            items.append(
                "<div class='graph-fallback-item'>"
                f"<div class='graph-fallback-top'><strong>{meta}</strong>{badge}</div>"
                f"<p>{html.escape(detail)}</p>"
                "</div>"
            )
        cards.append(
            "<section class='graph-fallback-group'>"
            f"<h3>{html.escape(label)}</h3>"
            f"<div class='graph-fallback-stack'>{''.join(items)}</div>"
            "</section>"
        )
    return "".join(cards)


def _format_tools_summary(tools: list[str]) -> str:
    if not tools:
        return "No direct tools"
    if len(tools) <= 2:
        return ", ".join(_humanize_slug(tool) for tool in tools)
    return f"{len(tools)} tools: " + ", ".join(_humanize_slug(tool) for tool in tools[:2])


def _format_capabilities_table(graph_payload: dict[str, Any]) -> str:
    capabilities = sorted(
        (
            node
            for node in graph_payload.get("nodes", [])
            if node.get("kind") == "capability"
        ),
        key=lambda item: str(item.get("label") or ""),
    )
    if not capabilities:
        return "<p class='runtime-empty'>No capabilities were detected in this runtime.</p>"

    rows: list[str] = []
    for node in capabilities:
        metadata = node.get("metadata") or {}
        tools = sorted(str(tool) for tool in metadata.get("tools", []) if str(tool).strip())
        plugins = sorted(str(plugin) for plugin in metadata.get("plugins", []) if str(plugin).strip())
        tags = [str(tag) for tag in node.get("tags", []) if str(tag).strip()]
        source = str(metadata.get("source") or ("plugin" if plugins else "runtime"))
        details = []
        if plugins:
            details.append(f"Implemented by: {', '.join(plugins)}")
        if tools:
            details.append(f"Tools: {', '.join(tools)}")
        if tags:
            details.append(f"Tags: {', '.join(tags)}")
        details.append(str(node.get("detail") or "No details available."))
        rows.append(
            "<tr>"
            "<td>"
            f"<strong>{html.escape(str(node.get('label') or 'Unknown'))}</strong>"
            f"<span>{html.escape(str(node.get('status') or 'available'))}</span>"
            "</td>"
            f"<td>{html.escape(_truncate_text(str(node.get('detail') or ''), 120))}</td>"
            f"<td><span class='runtime-tool-summary'>{html.escape(_format_tools_summary(tools))}</span></td>"
            f"<td>{html.escape(_humanize_slug(source))}</td>"
            "<td>"
            "<details class='runtime-details'>"
            "<summary>Details</summary>"
            f"<p>{html.escape(' | '.join(details))}</p>"
            "</details>"
            "</td>"
            "</tr>"
        )
    return (
        "<div class='runtime-table-shell'>"
        "<table class='runtime-table'>"
        "<thead><tr><th>Capability</th><th>Summary</th><th>Tools</th><th>Source</th><th>More</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
        "</div>"
    )


def _format_surfaces_table(graph_payload: dict[str, Any]) -> str:
    surfaces = sorted(
        (
            node
            for node in graph_payload.get("nodes", [])
            if node.get("kind") == "surface"
        ),
        key=lambda item: (str(item.get("category") or ""), str(item.get("label") or "")),
    )
    if not surfaces:
        return "<p class='runtime-empty'>No public surfaces were detected in this runtime.</p>"

    rows: list[str] = []
    for node in surfaces:
        href = str(node.get("href") or "").strip()
        category = str(node.get("category") or "surface")
        link = (
            f"<a href='{html.escape(href)}' target='_blank' rel='noopener'>{html.escape(href)}</a>"
            if href
            else "Unavailable"
        )
        rows.append(
            "<tr>"
            "<td>"
            f"<strong>{html.escape(str(node.get('label') or 'Unknown'))}</strong>"
            f"<span>{html.escape(_humanize_slug(category))}</span>"
            "</td>"
            f"<td>{link}</td>"
            f"<td>{html.escape(_truncate_text(str(node.get('detail') or ''), 130))}</td>"
            f"<td>{html.escape(str(node.get('status') or 'public'))}</td>"
            "</tr>"
        )
    return (
        "<div class='runtime-table-shell'>"
        "<table class='runtime-table surface-table'>"
        "<thead><tr><th>Surface</th><th>URI</th><th>Purpose</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
        "</div>"
    )


def _canonical_urls() -> list[str]:
    return [
        f"{BASE_URL}/",
        f"{BASE_URL}/og-image.svg",
        f"{BASE_URL}/agent-graph.json",
        f"{BASE_URL}/.well-known/agent-card.json",
        f"{BASE_URL}/.well-known/agent-skills/index.json",
        f"{BASE_URL}/.well-known/did.json",
        f"{BASE_URL}/.well-known/agent-registration.json",
        f"{BASE_URL}/.well-known/api-catalog",
    ]


def _build_sitemap_xml() -> str:
    urls = "".join(f"<url><loc>{html.escape(url)}</loc></url>" for url in _canonical_urls())
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        f"{urls}"
        "</urlset>"
    )


def _build_robots_txt() -> str:
    content_signal = os.environ.get(
        "CONTENT_SIGNAL_POLICY", "ai-train=no, search=yes, ai-input=no"
    )
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /internal/",
        "Disallow: /files/",
        "",
        "User-agent: GPTBot",
        "Allow: /",
        "Disallow: /internal/",
        "Disallow: /files/",
        "",
        "User-agent: OAI-SearchBot",
        "Allow: /",
        "Disallow: /internal/",
        "Disallow: /files/",
        "",
        "User-agent: Claude-Web",
        "Allow: /",
        "Disallow: /internal/",
        "Disallow: /files/",
        "",
        "User-agent: Google-Extended",
        "Allow: /",
        "Disallow: /internal/",
        "Disallow: /files/",
        "",
        f"Content-Signal: {content_signal}",
        f"Sitemap: {BASE_URL}/sitemap.xml",
    ]
    return "\n".join(lines) + "\n"


def _homepage_link_header() -> str:
    return ", ".join(
        [
            '</agent-graph.json>; rel="alternate"; type="application/json"',
            '</.well-known/api-catalog>; rel="api-catalog"',
            '</.well-known/agent-card.json>; rel="service-desc"',
            '</.well-known/agent-skills/index.json>; rel="service-doc"',
        ]
    )


def _render_home_markdown(
    agent_name: str,
    agent_description: str,
    did: str,
    wallet_address: str,
    sbc_balance: str,
    rusd_balance: str,
    explorer_link: str,
    published_skills: list[dict],
    skill_summary: str,
) -> str:
    lines = [
        f"# {agent_name}",
        "",
        agent_description,
        "",
        "## Identity",
        f"- **DID:** `{did}`",
        f"- **Base URL:** {BASE_URL}",
        "",
        "## Wallet",
        f"- **Address:** `{wallet_address}`",
        f"- **SBC:** `{sbc_balance}`",
        f"- **RUSD:** `{rusd_balance}`",
        f"- **Explorer:** {explorer_link}",
        "",
        "## Discovery",
        f"- Agent Card: {BASE_URL}/.well-known/agent-card.json",
        f"- Skills Index: {BASE_URL}/.well-known/agent-skills/index.json",
        f"- DID Document: {BASE_URL}/.well-known/did.json",
        f"- ERC-8004 Registration: {BASE_URL}/.well-known/agent-registration.json",
        f"- API Catalog: {BASE_URL}/.well-known/api-catalog",
        f"- Agent Graph: {BASE_URL}/agent-graph.json",
        f"- Sitemap: {BASE_URL}/sitemap.xml",
        "",
        "## Skills",
        f"- {skill_summary}",
    ]
    for skill in published_skills:
        skill_name = _humanize_slug(str(skill.get("name") or "unknown"))
        description = str(skill.get("description") or "Published capability")
        lines.append(f"- **{skill_name}:** {description}")
    return "\n".join(lines).strip() + "\n"


def _build_social_svg(
    agent_name: str, description: str, skill_labels: list[str]
) -> str:
    chips = skill_labels[:3] or ["A2A Discovery", "Radius Wallet", "Published Skills"]
    chip_markup = []
    x = 68
    y = 316
    for chip in chips:
        width = max(132, 18 + len(chip) * 10)
        chip_markup.append(
            (
                f"<rect x='{x}' y='{y}' width='{width}' height='46' rx='23' fill='rgba(255,255,255,0.14)' "
                "stroke='rgba(255,255,255,0.18)'/>"
                f"<text x='{x + 22}' y='{y + 29}' fill='white' font-size='20' font-family='Instrument Sans, Arial, sans-serif'>{html.escape(chip)}</text>"
            )
        )
        x += width + 16
    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='630' viewBox='0 0 1200 630' role='img' aria-label='{html.escape(agent_name)} social preview'>
  <defs>
    <linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0%' stop-color='#201e25'/>
      <stop offset='55%' stop-color='#44332c'/>
      <stop offset='100%' stop-color='#eb6359'/>
    </linearGradient>
    <radialGradient id='glow' cx='0' cy='0' r='1' gradientUnits='userSpaceOnUse' gradientTransform='translate(1050 110) rotate(140) scale(410 320)'>
      <stop offset='0%' stop-color='rgba(255,255,255,0.24)'/>
      <stop offset='100%' stop-color='rgba(255,255,255,0)'/>
    </radialGradient>
  </defs>
  <rect width='1200' height='630' fill='url(#bg)'/>
  <rect width='1200' height='630' fill='url(#glow)'/>
  <rect x='44' y='44' width='1112' height='542' rx='32' fill='rgba(255,255,255,0.07)' stroke='rgba(255,255,255,0.14)'/>
  <text x='68' y='116' fill='rgba(255,255,255,0.78)' font-size='24' font-family='Instrument Sans, Arial, sans-serif' letter-spacing='4'>AI Agent on Radius</text>
  <text x='68' y='220' fill='white' font-size='72' font-family='Instrument Sans, Arial, sans-serif' font-weight='600'>{html.escape(_truncate_text(agent_name, 28))}</text>
  <text x='68' y='276' fill='rgba(255,255,255,0.84)' font-size='30' font-family='Instrument Sans, Arial, sans-serif'>{html.escape(_truncate_text(description, 78))}</text>
  {"".join(chip_markup)}
  <text x='68' y='504' fill='rgba(255,255,255,0.72)' font-size='26' font-family='Instrument Sans, Arial, sans-serif'>Human-readable capability overview plus canonical /.well-known discovery docs.</text>
  <text x='68' y='554' fill='rgba(255,255,255,0.6)' font-size='22' font-family='Instrument Sans, Arial, sans-serif'>{html.escape(BASE_URL)}</text>
</svg>"""


@app.get("/og-image.svg")
async def social_preview_image():
    agent_card = _build_agent_card_payload()
    agent_name = agent_card["name"]
    description = agent_card["description"]
    skill_labels = [
        _humanize_slug(skill.get("name", "")) for skill in agent_card.get("skills", [])
    ]
    return Response(
        content=_build_social_svg(agent_name, description, skill_labels),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/robots.txt")
async def robots_txt():
    return PlainTextResponse(
        _build_robots_txt(),
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/sitemap.xml")
async def sitemap_xml():
    return Response(
        content=_build_sitemap_xml(),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/agent-graph.json")
async def agent_graph():
    return JSONResponse(
        _build_agent_graph_payload(),
        headers={"Cache-Control": "public, max-age=60"},
    )


@app.get("/static/{asset_path:path}")
async def static_asset(asset_path: str):
    requested = Path(asset_path)
    candidate = (STATIC_ROOT / requested).resolve()
    try:
        candidate.relative_to(STATIC_ROOT)
    except ValueError:
        return PlainTextResponse("Forbidden", status_code=403)
    if not candidate.exists() or not candidate.is_file():
        return PlainTextResponse("Not Found", status_code=404)
    media_type, _ = mimetypes.guess_type(candidate.name)
    if candidate.suffix == ".js":
        media_type = "text/javascript; charset=utf-8"
    elif candidate.suffix == ".json":
        media_type = "application/json; charset=utf-8"
    return Response(
        content=candidate.read_bytes(),
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/")
async def index(request: Request):
    agent_card = _build_agent_card_payload()
    agent_name = agent_card["name"]
    agent_description = agent_card["description"]
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
    explorer_link = wallet_explorer_link(wallet_summary.get("address"))

    published_skills = skills_index.get("skills", [])
    graph_payload = _build_agent_graph_payload()
    capability_table_html = _format_capabilities_table(graph_payload)
    surface_table_html = _format_surfaces_table(graph_payload)
    graph_stats = graph_payload.get("stats", {})
    graph_node_count = int(graph_stats.get("node_count", 0) or 0)
    graph_edge_count = int(graph_stats.get("edge_count", 0) or 0)
    graph_external_count = int(graph_stats.get("external_count", 0) or 0)
    graph_internal_count = int(graph_stats.get("internal_count", 0) or 0)
    graph_published_skill_count = int(graph_stats.get("published_skill_count", 0) or 0)
    graph_capability_count = int(graph_stats.get("capability_count", 0) or 0)
    graph_tool_count = int(graph_stats.get("tool_count", 0) or 0)
    wallet_note = (
        f"<p class='note'>Wallet data unavailable: {html.escape(wallet_error)}</p>"
        if wallet_error
        else ""
    )
    radius_site = "https://radiustech.xyz"
    template_repo = "https://github.com/radius-workshop/radius-hermes-railway-template"
    page_title = f"{agent_name} | Radius Hermes Agent"
    og_title = f"{agent_name} | Public A2A Discovery"
    og_description = _truncate_text(
        f"{agent_description} Human-readable capabilities plus canonical /.well-known discovery documents for A2A clients, registries, and operator tooling.",
        180,
    )
    og_image_url = f"{BASE_URL}/og-image.svg"
    published_count = len(published_skills)
    graph_summary = f"{graph_node_count} nodes and {graph_edge_count} links generated from the live runtime."
    import_map_json = json.dumps(
        {
            "imports": {
                "three": "https://cdn.jsdelivr.net/npm/three@0.184.0/build/three.module.js",
                "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.184.0/examples/jsm/",
            }
        },
        separators=(",", ":"),
    )
    skill_summary = (
        f"{published_count} published skill{'s' if published_count != 1 else ''} exposed via the public skills index."
        if published_count
        else "No published skills yet, but the canonical discovery documents are available now."
    )

    markdown = _render_home_markdown(
        agent_name=agent_name,
        agent_description=agent_description,
        did=did,
        wallet_address=wallet_address,
        sbc_balance=sbc_balance,
        rusd_balance=rusd_balance,
        explorer_link=explorer_link,
        published_skills=published_skills,
        skill_summary=skill_summary,
    )
    if "text/markdown" in request.headers.get("accept", "").lower():
        token_count = len(markdown.split())
        response = Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"x-markdown-tokens": str(token_count)},
        )
        response.headers["Link"] = _homepage_link_header()
        return response

    response = HTMLResponse(
        f"""<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='UTF-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1.0'>
  <title>{html.escape(page_title)}</title>
  <meta name='description' content='{html.escape(og_description)}'>
  <meta property='og:title' content='{html.escape(og_title)}'>
  <meta property='og:description' content='{html.escape(og_description)}'>
  <meta property='og:type' content='website'>
  <meta property='og:url' content='{html.escape(BASE_URL)}/'>
  <meta property='og:image' content='{html.escape(og_image_url)}'>
  <meta property='og:image:type' content='image/svg+xml'>
  <meta name='twitter:card' content='summary_large_image'>
  <meta name='twitter:title' content='{html.escape(og_title)}'>
  <meta name='twitter:description' content='{html.escape(og_description)}'>
  <meta name='twitter:image' content='{html.escape(og_image_url)}'>
  <link rel='canonical' href='{html.escape(BASE_URL)}/'>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@300;400;500;600;700&display=swap");

    :root {{
      --background: #fff;
      --foreground: #1f1f25;
      --card: rgba(255, 255, 255, 0.82);
      --card-strong: rgba(255, 255, 255, 0.94);
      --line: rgba(31, 31, 37, 0.1);
      --line-strong: rgba(31, 31, 37, 0.18);
      --muted: rgba(31, 31, 37, 0.64);
      --muted-soft: rgba(31, 31, 37, 0.48);
      --primary: #eb6359;
      --primary-soft: rgba(235, 99, 89, 0.12);
      --secondary: #e2ddd9;
      --shadow: 0 24px 80px rgba(65, 45, 36, 0.12);
      --radius: 20px;
    }}
    * {{ box-sizing: border-box; }}

    html {{
      font-size: 16px;
    }}

    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Instrument Sans", sans-serif;
      color: var(--foreground);
      background:
        radial-gradient(circle at top left, rgba(235, 99, 89, 0.14), transparent 28%),
        radial-gradient(circle at 85% 18%, rgba(226, 221, 217, 0.92), transparent 26%),
        linear-gradient(180deg, #fffaf7 0%, #f6f1ec 48%, #efe7df 100%);
      padding: 24px;
      position: relative;
      overflow-x: hidden;
    }}

    body::before,
    body::after {{
      content: "";
      position: fixed;
      inset: auto;
      pointer-events: none;
      border-radius: 999px;
      filter: blur(10px);
      opacity: 0.75;
    }}

    body::before {{
      width: 22rem;
      height: 22rem;
      top: -6rem;
      right: -5rem;
      background: rgba(235, 99, 89, 0.16);
    }}

    body::after {{
      width: 18rem;
      height: 18rem;
      left: -6rem;
      bottom: 5rem;
      background: rgba(226, 221, 217, 0.9);
    }}

    a {{
      color: inherit;
      text-decoration: none;
    }}

    .wrap {{
      max-width: 1440px;
      margin: 0 auto;
      position: relative;
      z-index: 1;
    }}

    .site-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
      padding: 12px 16px;
      border: 1px solid rgba(255, 255, 255, 0.6);
      border-radius: 22px;
      background: rgba(255, 255, 255, 0.62);
      box-shadow: 0 18px 50px rgba(65, 45, 36, 0.08);
      backdrop-filter: blur(18px);
      position: relative;
      z-index: 30;
    }}

    .brand {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      font-weight: 600;
      letter-spacing: -0.02em;
    }}

    .brand-mark {{
      width: 38px;
      height: 27px;
      color: var(--foreground);
      flex-shrink: 0;
    }}

    .brand-copy {{
      display: flex;
      flex-direction: column;
      min-width: 0;
    }}

    .brand-name {{
      font-size: 1rem;
      line-height: 1;
      color: var(--foreground);
    }}

    .brand-subtitle {{
      margin-top: 3px;
      font-size: 11px;
      line-height: 1.2;
      color: var(--muted);
      font-weight: 500;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .site-nav {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
    }}

    .nav-shell {{
      position: relative;
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .nav-menu {{
      margin: 0;
      display: none;
    }}

    .desktop-cta {{
      display: inline-flex;
    }}

    .desktop-only {{
      display: inline-flex;
    }}

    .mobile-only {{
      display: none;
    }}

    .discovery-menu {{
      position: relative;
      margin: 0;
    }}

    .resources-menu {{
      position: relative;
      margin: 0;
    }}

    .discovery-toggle {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 0 15px;
      border-radius: 999px;
      border: 1px solid rgba(31, 31, 37, 0.1);
      background: rgba(255, 255, 255, 0.78);
      color: rgba(31, 31, 37, 0.86);
      cursor: pointer;
      list-style: none;
      user-select: none;
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
      transition: border-color 160ms ease, background 160ms ease, color 160ms ease, box-shadow 160ms ease;
    }}

    .resources-toggle {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 0 15px;
      border-radius: 999px;
      border: 1px solid rgba(31, 31, 37, 0.1);
      background: rgba(255, 255, 255, 0.78);
      color: rgba(31, 31, 37, 0.86);
      cursor: pointer;
      list-style: none;
      user-select: none;
      font-size: 13px;
      font-weight: 500;
      white-space: nowrap;
      transition: border-color 160ms ease, background 160ms ease, color 160ms ease, box-shadow 160ms ease;
    }}

    .discovery-toggle::-webkit-details-marker {{
      display: none;
    }}

    .resources-toggle::-webkit-details-marker {{
      display: none;
    }}

    .discovery-panel {{
      position: absolute;
      top: calc(100% + 12px);
      right: 0;
      width: min(34rem, calc(100vw - 48px));
      padding: 14px;
      border-radius: 20px;
      border: 1px solid rgba(31, 31, 37, 0.08);
      background: #fff;
      box-shadow: 0 28px 70px rgba(31, 31, 37, 0.16);
      z-index: 20;
      transform-origin: top right;
      animation: menu-rise 140ms ease-out;
    }}

    .resources-panel {{
      position: absolute;
      top: calc(100% + 12px);
      right: 0;
      width: 13rem;
      padding: 10px;
      border-radius: 18px;
      border: 1px solid rgba(31, 31, 37, 0.08);
      background: #fff;
      box-shadow: 0 24px 60px rgba(31, 31, 37, 0.14);
      z-index: 20;
      transform-origin: top right;
      animation: menu-rise 140ms ease-out;
    }}

    .discovery-menu[open] .discovery-toggle,
    .resources-menu[open] .resources-toggle {{
      border-color: rgba(235, 99, 89, 0.38);
      background: #fff;
      color: var(--foreground);
      box-shadow: 0 0 0 4px rgba(235, 99, 89, 0.1);
    }}

    .resources-links {{
      display: grid;
      gap: 8px;
    }}

    .resources-link {{
      display: flex;
      align-items: center;
      min-height: 40px;
      padding: 0 12px;
      border-radius: 12px;
      border: 1px solid rgba(31, 31, 37, 0.08);
      background: #fff;
      font-size: 13px;
      font-weight: 500;
      transition: background 160ms ease, border-color 160ms ease, transform 160ms ease;
    }}

    .resources-link:hover {{
      background: rgba(235, 99, 89, 0.08);
      border-color: rgba(235, 99, 89, 0.2);
      transform: translateY(-1px);
    }}

    .mobile-nav-section {{
      display: none;
    }}

    .mobile-nav-label {{
      margin: 2px 2px 0;
      color: var(--muted-soft);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .mobile-nav-stack,
    .mobile-fact-list,
    .mobile-resource-links {{
      display: grid;
      gap: 8px;
    }}

    .mobile-nav-divider {{
      height: 1px;
      margin: 2px 0;
      background: rgba(31, 31, 37, 0.08);
    }}

    @keyframes menu-rise {{
      from {{
        opacity: 0;
        transform: translateY(-6px) scale(0.985);
      }}
      to {{
        opacity: 1;
        transform: translateY(0) scale(1);
      }}
    }}

    .discovery-panel-head {{
      margin-bottom: 10px;
    }}

    .discovery-panel-head h2 {{
      margin: 0;
      font-size: 1rem;
      letter-spacing: -0.02em;
    }}

    .discovery-panel-head p {{
      margin-top: 4px;
      font-size: 0.88rem;
    }}

    .nav-toggle {{
      display: none;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      min-width: 40px;
      border: 1px solid rgba(31, 31, 37, 0.12);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.76);
      color: var(--foreground);
      cursor: pointer;
      list-style: none;
      user-select: none;
      padding: 0 12px;
      font-size: 13px;
      font-weight: 600;
      gap: 10px;
    }}

    .mobile-menu-panel {{
      display: none;
    }}

    .nav-toggle::-webkit-details-marker {{
      display: none;
    }}

    .nav-toggle-bars {{
      display: inline-grid;
      gap: 4px;
    }}

    .nav-toggle-bars span {{
      display: block;
      width: 16px;
      height: 2px;
      border-radius: 999px;
      background: currentColor;
    }}

    .nav-link,
    .nav-cta {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      border-radius: 999px;
      padding: 0 14px;
      transition: transform 160ms ease, border-color 160ms ease, background 160ms ease, color 160ms ease;
      white-space: nowrap;
    }}

    .nav-link {{
      border: 1px solid rgba(31, 31, 37, 0.1);
      background: rgba(255, 255, 255, 0.66);
      color: rgba(31, 31, 37, 0.82);
      font-size: 13px;
      font-weight: 500;
    }}

    .nav-link:hover {{
      transform: translateY(-1px);
      border-color: rgba(31, 31, 37, 0.18);
      background: rgba(255, 255, 255, 0.92);
    }}

    .nav-cta {{
      position: relative;
      overflow: hidden;
      isolation: isolate;
      padding: 0 17px;
      border: 1px solid rgba(255,255,255,0.68);
      background:
        radial-gradient(circle at 12% 18%, rgba(255,255,255,0.96), transparent 18%),
        radial-gradient(circle at 24% 78%, rgba(79,209,197,0.92), transparent 27%),
        radial-gradient(circle at 50% 22%, rgba(255,205,94,0.95), transparent 27%),
        radial-gradient(circle at 72% 72%, rgba(235,99,89,0.95), transparent 30%),
        radial-gradient(circle at 88% 20%, rgba(111,92,255,0.92), transparent 26%),
        linear-gradient(110deg, #121216, #37242d 36%, #0f0f12);
      color: #fff;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.01em;
      text-shadow: 0 1px 12px rgba(0,0,0,0.32);
      box-shadow:
        0 16px 34px rgba(235,99,89,0.2),
        0 8px 20px rgba(111,92,255,0.14),
        inset 0 1px 0 rgba(255,255,255,0.58),
        inset 0 -14px 24px rgba(0,0,0,0.14);
    }}

    .nav-cta::before {{
      content: "";
      position: absolute;
      inset: -55%;
      z-index: -1;
      background:
        conic-gradient(from 120deg, rgba(255,255,255,0), rgba(255,255,255,0.72), rgba(255,255,255,0) 32%),
        radial-gradient(circle, rgba(255,255,255,0.24), transparent 54%);
      transform: rotate(12deg);
      transition: transform 420ms ease;
      mix-blend-mode: screen;
    }}

    .nav-cta:hover {{
      transform: translateY(-2px);
      box-shadow:
        0 20px 42px rgba(235,99,89,0.26),
        0 12px 26px rgba(79,209,197,0.16),
        0 0 0 4px rgba(255,255,255,0.42),
        inset 0 1px 0 rgba(255,255,255,0.7),
        inset 0 -14px 24px rgba(0,0,0,0.1);
    }}

    .nav-cta:hover::before {{
      transform: rotate(32deg) translateX(8%);
    }}

    .hero-shell {{
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(255, 255, 255, 0.65);
      border-radius: 28px;
      background:
        linear-gradient(135deg, rgba(255,255,255,0.86), rgba(255,255,255,0.58)),
        linear-gradient(160deg, rgba(235,99,89,0.06), rgba(226,221,217,0.18));
      box-shadow: var(--shadow);
      padding: clamp(1rem, 1.5vw, 1.35rem);
      backdrop-filter: blur(18px);
      z-index: 1;
    }}

    .hero-shell::before {{
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(115deg, transparent 0%, rgba(235, 99, 89, 0.08) 48%, transparent 100%);
      pointer-events: none;
    }}

    .hero {{
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 14px;
      align-items: stretch;
    }}

    .card {{
      position: relative;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      backdrop-filter: blur(12px);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
    }}

    .hero-main {{
      background:
        linear-gradient(180deg, var(--card-strong), rgba(255,255,255,0.78)),
        linear-gradient(145deg, rgba(235,99,89,0.06), transparent);
    }}

    .hero-side {{
      background:
        linear-gradient(180deg, rgba(255,255,255,0.9), rgba(255,255,255,0.78)),
        linear-gradient(135deg, rgba(235,99,89,0.08), transparent);
    }}

    h1 {{
      margin: 0;
      max-width: 11ch;
      font-size: clamp(2.4rem, 5vw, 4.4rem);
      line-height: 0.94;
      letter-spacing: -0.04em;
      font-weight: 500;
    }}

    p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
      font-weight: 400;
      font-size: 0.95rem;
    }}

    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
      color: var(--primary);
      letter-spacing: 0.1em;
      text-transform: uppercase;
      font-size: 11px;
      font-weight: 600;
    }}

    .eyebrow::before {{
      content: "";
      width: 18px;
      height: 1px;
      background: currentColor;
      opacity: 0.72;
    }}

    .lede {{
      max-width: 38rem;
      margin-top: 10px;
      font-size: clamp(0.94rem, 1.1vw, 1.02rem);
    }}

    .hero-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }}

    .pill {{
      display: inline-flex;
      align-items: center;
      border: 1px solid rgba(31, 31, 37, 0.1);
      border-radius: 999px;
      padding: 7px 11px;
      background: rgba(255, 255, 255, 0.72);
      color: rgba(31, 31, 37, 0.78);
      font-size: 12px;
      font-weight: 500;
    }}

    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 16px;
    }}

    .stat {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.58);
      min-height: 96px;
    }}

    .hero-side .stat {{
      background: rgba(255,255,255,0.05);
      border-color: rgba(255,255,255,0.1);
    }}

    .label {{
      display: block;
      color: var(--muted-soft);
      font-size: 11px;
      margin-bottom: 7px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}

    .value {{
      font-size: clamp(1.2rem, 1.6vw, 1.6rem);
      color: var(--foreground);
      word-break: break-word;
      letter-spacing: -0.03em;
    }}

    .hero-side .value {{
      color: #fff;
    }}

    .value.small {{
      font-size: 12px;
      line-height: 1.45;
      letter-spacing: 0;
    }}

    .capability-section {{
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid rgba(31, 31, 37, 0.08);
    }}

    .capability-copy {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 10px;
    }}

    .capability-copy p {{
      max-width: 42rem;
    }}

    .skill-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}

    .skill-grid.compact {{
      grid-template-columns: 1fr;
    }}

    .skill-card {{
      padding: 14px;
      border-radius: 16px;
      border: 1px solid rgba(31, 31, 37, 0.1);
      background: rgba(255, 255, 255, 0.58);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.65);
    }}

    .skill-card.empty {{
      grid-column: 1 / -1;
    }}

    .skill-card p {{
      font-size: 0.88rem;
      line-height: 1.45;
    }}

    .skill-card-top {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }}

    .skill-card h3 {{
      margin: 0;
      font-size: 1rem;
      line-height: 1.1;
      letter-spacing: -0.02em;
      font-weight: 600;
    }}

    .mini-pills {{
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 5px;
    }}

    .mini-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      background: rgba(235, 99, 89, 0.12);
      color: rgba(31, 31, 37, 0.82);
      font-size: 11px;
      font-weight: 600;
    }}

    .skill-slug {{
      margin-top: 8px;
      color: var(--muted-soft);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}

    .discovery-links {{
      display: grid;
      gap: 8px;
      margin-top: 14px;
    }}

    .discovery-card {{
      display: block;
      color: inherit;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 13px;
      background: rgba(255,255,255,0.04);
      transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
    }}

    .discovery-card:hover {{
      transform: translateY(-1px);
      border-color: var(--line-strong);
      background: rgba(255,255,255,0.1);
    }}

    .discovery-card span {{
      display: block;
      color: var(--muted-soft);
      margin-bottom: 4px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}

    .discovery-card strong {{
      color: var(--primary);
      font-size: 12px;
      font-weight: 500;
      word-break: break-all;
    }}

    .discovery-card p {{
      margin-top: 6px;
      font-size: 12px;
      color: inherit;
      line-height: 1.5;
    }}

    .fact-list {{
      display: grid;
      gap: 8px;
      margin-top: 14px;
    }}

    .fact-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(255,255,255,0.7);
      border: 1px solid rgba(31,31,37,0.08);
    }}

    .fact-row span {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted-soft);
    }}

    .fact-row strong {{
      color: var(--foreground);
      font-size: 12px;
      line-height: 1.35;
      word-break: break-word;
      text-align: right;
    }}

    .note {{
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 12px;
      background: var(--primary-soft);
      color: rgba(31, 31, 37, 0.72);
      font-size: 12px;
      line-height: 1.45;
    }}

    .header-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}

    .agent-surface {{
      margin-top: 14px;
    }}

    .agent-card-shell {{
      position: relative;
      border: 0;
      border-radius: 0;
      padding: 0;
      background: transparent;
      box-shadow: none;
      backdrop-filter: none;
    }}

    .agent-pdp {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(22rem, 22rem);
      gap: 0;
      align-items: start;
      border-radius: 24px;
      overflow: hidden;
      border: 0;
      background: rgba(255,255,255,0.62);
      box-shadow: var(--shadow);
    }}

    .agent-visual-panel {{
      padding: 16px;
      border-right: 1px solid rgba(31,31,37,0.08);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.9), rgba(255,252,244,0.86)),
        linear-gradient(135deg, rgba(242,196,100,0.08), rgba(235,99,89,0.04));
    }}

    .graph-toolbar {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 10px;
      margin-top: 12px;
      margin-bottom: 0;
      align-items: center;
    }}

    .graph-search-input,
    .graph-filter-select,
    .graph-filter-reset {{
      min-height: 44px;
      border-radius: 999px;
      border: 1px solid rgba(31,31,37,0.08);
      background: rgba(255,255,255,0.86);
      color: var(--foreground);
      font: inherit;
      font-size: 14px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.82);
    }}

    .graph-search-input {{
      width: 100%;
      padding: 0 16px;
    }}

    .graph-filter-select {{
      min-width: 10rem;
      padding: 0 14px;
      appearance: none;
    }}

    .graph-filter-reset {{
      padding: 0 16px;
      cursor: pointer;
      transition: transform 160ms ease, background 160ms ease;
    }}

    .graph-filter-reset:hover {{
      transform: translateY(-1px);
      background: rgba(31,31,37,0.06);
    }}

    .graph-toolbar-meta {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 10px;
      margin-bottom: 0;
      color: rgba(64, 46, 34, 0.68);
      font-size: 12px;
    }}

    .graph-result-count {{
      display: inline-flex;
      align-items: center;
      min-height: 26px;
      padding: 0 10px;
      border-radius: 999px;
      border: 1px solid rgba(89,59,34,0.08);
      background: rgba(255,255,255,0.72);
      font-weight: 600;
    }}

    .graph-toolbar-hint {{
      white-space: nowrap;
    }}

    .agent-visual-panel .graph-canvas-shell {{
      height: clamp(27rem, 54vh, 34rem);
      min-height: 0;
      border-radius: 22px;
    }}

    .graph-selection-dock {{
      margin-top: 12px;
      transform: translateY(-2px);
    }}

    .graph-selection-dock .agent-detail-card {{
      border-radius: 20px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.94), rgba(255,249,236,0.84)),
        linear-gradient(135deg, rgba(242,196,100,0.14), rgba(235,99,89,0.06));
      box-shadow: 0 18px 42px rgba(76,56,41,0.1), inset 0 1px 0 rgba(255,255,255,0.86);
    }}

    .agent-detail-rail {{
      display: grid;
      gap: 16px;
      min-width: 0;
      padding: 18px;
      background: rgba(255,255,255,0.9);
    }}

    .agent-detail-title h1 {{
      max-width: none;
      margin: 0;
      font-size: clamp(2.1rem, 4vw, 3.7rem);
    }}

    .agent-detail-title p {{
      margin-top: 8px;
    }}

    .agent-detail-summary {{
      padding-top: 12px;
      border-top: 1px solid rgba(31,31,37,0.08);
      font-size: 0.96rem;
      color: rgba(31,31,37,0.72);
    }}

    .agent-cta {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 54px;
      padding: 0 18px;
      border-radius: 999px;
      font-size: 15px;
      font-weight: 600;
      letter-spacing: -0.01em;
      position: relative;
      overflow: hidden;
      isolation: isolate;
      transition: transform 180ms ease, background 180ms ease, color 180ms ease, border-color 180ms ease, box-shadow 180ms ease;
    }}

    .agent-cta:hover {{
      transform: translateY(-2px);
    }}

    .agent-cta.primary {{
      border: 1px solid rgba(255,255,255,0.68);
      background:
        radial-gradient(circle at 12% 18%, rgba(255,255,255,0.96), transparent 18%),
        radial-gradient(circle at 24% 78%, rgba(79,209,197,0.92), transparent 27%),
        radial-gradient(circle at 50% 22%, rgba(255,205,94,0.95), transparent 27%),
        radial-gradient(circle at 72% 72%, rgba(235,99,89,0.95), transparent 30%),
        radial-gradient(circle at 88% 20%, rgba(111,92,255,0.92), transparent 26%),
        linear-gradient(110deg, #121216, #37242d 36%, #0f0f12);
      color: #fff;
      text-shadow: 0 1px 16px rgba(0,0,0,0.36);
      box-shadow:
        0 18px 38px rgba(235,99,89,0.22),
        0 10px 24px rgba(111,92,255,0.16),
        inset 0 1px 0 rgba(255,255,255,0.58),
        inset 0 -18px 30px rgba(0,0,0,0.16);
    }}

    .agent-cta.primary::before {{
      content: "";
      position: absolute;
      inset: -45%;
      z-index: -1;
      background:
        conic-gradient(from 120deg, rgba(255,255,255,0), rgba(255,255,255,0.72), rgba(255,255,255,0) 32%),
        radial-gradient(circle, rgba(255,255,255,0.26), transparent 54%);
      transform: rotate(12deg);
      transition: transform 420ms ease;
      mix-blend-mode: screen;
    }}

    .agent-cta.primary:hover {{
      box-shadow:
        0 24px 54px rgba(235,99,89,0.28),
        0 16px 36px rgba(79,209,197,0.18),
        0 0 0 5px rgba(255,255,255,0.44),
        inset 0 1px 0 rgba(255,255,255,0.7),
        inset 0 -18px 30px rgba(0,0,0,0.12);
    }}

    .agent-cta.primary:hover::before {{
      transform: rotate(32deg) translateX(8%);
    }}

    .agent-cta.secondary {{
      border: 1px solid rgba(31,31,37,0.08);
      background: rgba(31,31,37,0.04);
      color: var(--foreground);
    }}

    .agent-detail-table {{
      border-top: 1px solid rgba(31,31,37,0.08);
      border-bottom: 1px solid rgba(31,31,37,0.08);
    }}

    .agent-detail-row {{
      display: grid;
      grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
      gap: 12px;
      padding: 12px 0;
      border-bottom: 1px solid rgba(31,31,37,0.08);
    }}

    .agent-detail-row:last-child {{
      border-bottom: 0;
    }}

    .agent-detail-row span {{
      color: var(--muted-soft);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .agent-detail-row strong,
    .agent-detail-row a {{
      color: var(--foreground);
      font-size: 13px;
      font-weight: 500;
      line-height: 1.45;
      word-break: break-word;
      text-align: right;
    }}

    .agent-detail-card {{
      padding: 15px;
      border-radius: 22px;
      border: 1px solid rgba(31, 31, 37, 0.08);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.9), rgba(255,255,255,0.78)),
        linear-gradient(135deg, rgba(242,196,100,0.1), rgba(235,99,89,0.04));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.82);
    }}

    .agent-detail-card h2 {{
      margin: 0;
      font-size: 1rem;
      letter-spacing: -0.02em;
    }}

    .agent-detail-card p {{
      margin-top: 6px;
      font-size: 0.9rem;
    }}

    .agent-runtime-sections {{
      margin-top: 18px;
      display: grid;
      gap: 14px;
    }}

    .agent-runtime-head h2 {{
      margin: 0;
      font-size: 1.1rem;
      letter-spacing: -0.03em;
    }}

    .agent-runtime-head p {{
      margin-top: 6px;
      max-width: 40rem;
    }}

    .runtime-table-section {{
      display: grid;
      gap: 10px;
      padding: 15px;
      border-radius: 24px;
      border: 1px solid rgba(31,31,37,0.08);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.9), rgba(255,255,255,0.78)),
        linear-gradient(140deg, rgba(242,196,100,0.08), rgba(235,99,89,0.03));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.8);
    }}

    .runtime-table-head {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
    }}

    .runtime-table-head h3 {{
      margin: 0;
      font-size: 1rem;
      letter-spacing: -0.02em;
    }}

    .runtime-table-head p {{
      max-width: 38rem;
      font-size: 0.88rem;
    }}

    .runtime-table-shell {{
      overflow-x: auto;
      border-radius: 18px;
      border: 1px solid rgba(89,59,34,0.08);
      background: rgba(255,255,255,0.68);
    }}

    .runtime-table {{
      width: 100%;
      min-width: 720px;
      border-collapse: collapse;
    }}

    .runtime-table th,
    .runtime-table td {{
      padding: 13px 14px;
      border-bottom: 1px solid rgba(89,59,34,0.08);
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}

    .runtime-table th {{
      color: rgba(89, 59, 34, 0.62);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      background: rgba(255,248,232,0.72);
    }}

    .runtime-table tr:last-child td {{
      border-bottom: 0;
    }}

    .runtime-table td strong {{
      display: block;
      color: var(--foreground);
      font-size: 13px;
      line-height: 1.25;
    }}

    .runtime-table td span {{
      display: inline-flex;
      margin-top: 5px;
      color: rgba(89, 59, 34, 0.62);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}

    .runtime-table a {{
      color: var(--primary);
      word-break: break-all;
      font-weight: 600;
    }}

    .runtime-tool-summary {{
      margin-top: 0 !important;
      color: rgba(31,31,37,0.78) !important;
      letter-spacing: 0 !important;
      text-transform: none !important;
    }}

    .runtime-details summary {{
      cursor: pointer;
      color: var(--primary);
      font-weight: 700;
    }}

    .runtime-details p {{
      margin-top: 7px;
      font-size: 12px;
    }}

    .runtime-empty {{
      padding: 14px;
      border-radius: 16px;
      background: rgba(255,255,255,0.68);
    }}

    .runtime-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}

    .runtime-grid .graph-fallback-group {{
      padding: 15px;
      border-radius: 22px;
      border: 1px solid rgba(31,31,37,0.08);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.9), rgba(255,255,255,0.78)),
        linear-gradient(140deg, rgba(242,196,100,0.08), rgba(235,99,89,0.03));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.8);
    }}

    .page-tabs {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin: 14px 0;
      padding: 8px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid rgba(255, 255, 255, 0.72);
      box-shadow: 0 18px 44px rgba(65, 45, 36, 0.08);
      backdrop-filter: blur(16px);
    }}

    .page-tab {{
      appearance: none;
      border: 1px solid transparent;
      background: transparent;
      color: rgba(31, 31, 37, 0.66);
      min-height: 40px;
      padding: 0 16px;
      border-radius: 999px;
      font: inherit;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: background 160ms ease, color 160ms ease, border-color 160ms ease, transform 160ms ease;
    }}

    .page-tab:hover {{
      transform: translateY(-1px);
      color: var(--foreground);
    }}

    .page-tab.is-active {{
      background: rgba(235, 99, 89, 0.14);
      border-color: rgba(235, 99, 89, 0.2);
      color: var(--foreground);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.72);
    }}

    .tab-panel[hidden] {{
      display: none;
    }}

    .graph-shell {{
      margin-top: 14px;
      display: grid;
      gap: 16px;
    }}

    .graph-head {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 18px;
    }}

    .graph-head h2,
    .graph-collector h3,
    .graph-sidebar-card h3,
    .graph-fallback-group h3 {{
      margin: 0;
      letter-spacing: -0.02em;
    }}

    .graph-head p,
    .graph-sidebar-card p,
    .graph-fallback-item p {{
      margin-top: 6px;
      font-size: 0.9rem;
    }}

    .graph-kicker {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--primary);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .graph-kicker::before {{
      content: "";
      width: 18px;
      height: 1px;
      background: currentColor;
      opacity: 0.72;
    }}

    .graph-summary {{
      max-width: 30rem;
      font-size: 0.88rem;
      color: var(--muted);
      text-align: right;
    }}

    .graph-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.55fr) minmax(18rem, 0.85fr);
      gap: 16px;
      align-items: start;
    }}

    .graph-stage,
    .graph-sidebar {{
      min-width: 0;
    }}

    .graph-collector {{
      position: relative;
      overflow: hidden;
      padding: 14px;
      border-radius: 34px;
      background:
        linear-gradient(145deg, rgba(255, 244, 209, 0.98), rgba(242, 196, 100, 0.92) 28%, rgba(235, 99, 89, 0.88) 60%, rgba(73, 54, 43, 0.96));
      box-shadow: 0 30px 95px rgba(76, 56, 41, 0.2);
      isolation: isolate;
    }}

    .graph-collector::before {{
      content: "";
      position: absolute;
      inset: 1px;
      border-radius: 32px;
      background:
        linear-gradient(135deg, rgba(255,255,255,0.48), transparent 34%, rgba(255,255,255,0.12) 56%, transparent 80%),
        radial-gradient(circle at top left, rgba(255,255,255,0.78), transparent 32%);
      pointer-events: none;
      opacity: 0.95;
      mix-blend-mode: screen;
    }}

    .graph-collector::after {{
      content: "";
      position: absolute;
      top: 18px;
      right: 20px;
      width: 8rem;
      height: 8rem;
      border-radius: 999px;
      background: radial-gradient(circle, rgba(255,255,255,0.28), transparent 72%);
      filter: blur(10px);
      pointer-events: none;
    }}

    .graph-collector-inner {{
      position: relative;
      z-index: 1;
      padding: 18px;
      border-radius: 28px;
      border: 1px solid rgba(89, 59, 34, 0.22);
      background:
        linear-gradient(180deg, rgba(255, 249, 236, 0.96), rgba(252, 245, 226, 0.92) 30%, rgba(249, 238, 204, 0.94));
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.42), inset 0 1px 0 rgba(255,255,255,0.86);
    }}

    .graph-collector-top {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 12px;
    }}

    .graph-title-stack {{
      min-width: 0;
    }}

    .graph-card-type,
    .graph-rarity {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      border: 1px solid rgba(89, 59, 34, 0.12);
      background: rgba(255, 255, 255, 0.68);
      color: rgba(89, 59, 34, 0.78);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .graph-title-stack h2 {{
      margin: 10px 0 0;
      font-size: clamp(2rem, 3vw, 2.8rem);
      line-height: 0.96;
      letter-spacing: -0.04em;
    }}

    .graph-title-stack p {{
      max-width: 31rem;
      margin-top: 8px;
      color: rgba(64, 46, 34, 0.72);
    }}

    .graph-card-vitals {{
      display: grid;
      justify-items: end;
      gap: 8px;
      flex-shrink: 0;
    }}

    .graph-rarity {{
      background: rgba(255, 248, 232, 0.78);
    }}

    .graph-hp {{
      display: inline-flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      min-width: 9rem;
      padding: 10px 14px;
      border-radius: 18px;
      background: rgba(66, 43, 29, 0.92);
      color: #fff8eb;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.14);
    }}

    .graph-hp span {{
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      opacity: 0.72;
    }}

    .graph-hp strong {{
      font-size: clamp(1.7rem, 2.4vw, 2.2rem);
      line-height: 1;
      letter-spacing: -0.05em;
    }}

    .graph-art-shell {{
      padding: 12px;
      border-radius: 26px;
      border: 1px solid rgba(89, 59, 34, 0.16);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.84), rgba(255,248,233,0.58)),
        linear-gradient(135deg, rgba(242,196,100,0.18), rgba(235,99,89,0.08));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.72);
    }}

    .graph-art-banner {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 10px;
      color: rgba(89, 59, 34, 0.66);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .graph-art-banner span {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 9px;
      border-radius: 999px;
      border: 1px solid rgba(89, 59, 34, 0.08);
      background: rgba(255,255,255,0.58);
    }}

    .graph-canvas-shell {{
      position: relative;
      height: clamp(27rem, 54vh, 34rem);
      min-height: 0;
      border-radius: 24px;
      overflow: hidden;
      border: 1px solid rgba(89, 59, 34, 0.12);
      background:
        radial-gradient(circle at 50% 32%, rgba(255,255,255,0.94), rgba(255,248,223,0.86) 22%, rgba(246,214,142,0.48) 38%, rgba(235,160,108,0.22) 54%, rgba(66,43,29,0.1) 76%, rgba(255,254,247,0.96) 100%),
        linear-gradient(180deg, #fffef8 0%, #fff4d8 52%, #fbedd2 100%);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.4), 0 22px 52px rgba(76,56,41,0.18);
      isolation: isolate;
    }}

    .graph-canvas-shell::before {{
      content: "";
      position: absolute;
      inset: 0;
      background:
        radial-gradient(circle at 30% 28%, rgba(255,255,255,0.88), transparent 20%),
        linear-gradient(118deg, rgba(255,255,255,0.28), transparent 42%, rgba(255,255,255,0.14) 56%, transparent 76%);
      pointer-events: none;
      z-index: 1;
      mix-blend-mode: screen;
    }}

    .graph-canvas {{
      position: relative;
      z-index: 0;
      width: 100%;
      height: 100%;
      display: block;
    }}

    .graph-label-layer {{
      position: absolute;
      inset: 0;
      pointer-events: none;
      z-index: 2;
    }}

    .graph-label {{
      position: absolute;
      left: 0;
      top: 0;
      padding: 5px 9px;
      border-radius: 999px;
      background: rgba(255,255,255,0.94);
      border: 1px solid rgba(89,59,34,0.08);
      box-shadow: 0 10px 24px rgba(76,56,41,0.12);
      color: #3b2d25;
      font-size: 11px;
      line-height: 1;
      white-space: nowrap;
      transform: translate(-50%, -50%);
      transition: opacity 160ms ease, transform 160ms ease, box-shadow 160ms ease;
    }}

    .graph-label[data-variant='external'] {{
      border-color: rgba(235, 99, 89, 0.26);
      color: #b34d45;
    }}

    .graph-label[data-kind='interface'] {{
      border-color: rgba(127, 199, 255, 0.34);
      color: #276f9f;
    }}

    .graph-label[data-kind='capability'] {{
      border-color: rgba(114, 166, 64, 0.3);
      color: #496c28;
    }}

    .graph-label[data-kind='skill'] {{
      border-color: rgba(255, 184, 111, 0.42);
      color: #945928;
    }}

    .graph-label[data-kind='tool'] {{
      border-color: rgba(143, 140, 255, 0.36);
      color: #4b48a5;
    }}

    .graph-label[data-kind='channel'] {{
      border-color: rgba(99, 216, 198, 0.34);
      color: #287b70;
    }}

    .graph-label.is-hovered {{
      box-shadow: 0 0 0 4px rgba(242,196,100,0.18), 0 12px 26px rgba(76,56,41,0.16);
      transform: translate(-50%, -50%) scale(1.02);
    }}

    .graph-label.is-selected {{
      box-shadow: 0 0 0 4px rgba(235,99,89,0.14), 0 12px 28px rgba(76,56,41,0.18);
      transform: translate(-50%, -50%) scale(1.04);
    }}

    .graph-status {{
      position: absolute;
      left: 16px;
      right: 16px;
      bottom: 16px;
      z-index: 3;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(66,43,29,0.8);
      color: rgba(255,248,235,0.92);
      font-size: 12px;
      letter-spacing: 0.01em;
      backdrop-filter: blur(12px);
      box-shadow: 0 10px 24px rgba(66,43,29,0.2);
    }}

    .graph-art-caption {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
      margin-top: 12px;
      color: rgba(89, 59, 34, 0.72);
      font-size: 13px;
    }}

    .graph-art-caption strong {{
      color: #3b2d25;
      font-size: 13px;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}

    .graph-stat-strip {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }}

    .graph-stat-chip {{
      padding: 12px;
      border-radius: 18px;
      border: 1px solid rgba(89, 59, 34, 0.12);
      background: rgba(255,255,255,0.72);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.82);
    }}

    .graph-stat-chip span {{
      display: block;
      color: rgba(89, 59, 34, 0.62);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .graph-stat-chip strong {{
      display: block;
      margin-top: 6px;
      color: #2f231c;
      font-size: 1.35rem;
      letter-spacing: -0.04em;
    }}

    .graph-detail-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 12px;
    }}

    .graph-move-card,
    .graph-sidebar-card {{
      border-radius: 22px;
      border: 1px solid rgba(89, 59, 34, 0.12);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.86), rgba(255,255,255,0.76)),
        linear-gradient(135deg, rgba(242,196,100,0.12), rgba(235,99,89,0.05));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.82);
    }}

    .graph-move-card {{
      padding: 14px;
    }}

    .graph-move-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
      color: rgba(89, 59, 34, 0.62);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .graph-sidebar {{
      display: grid;
      gap: 12px;
    }}

    .graph-sidebar-card {{
      padding: 15px;
      border: 1px solid rgba(255, 255, 255, 0.68);
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }}

    .graph-note-grid {{
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }}

    .graph-note-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid rgba(89, 59, 34, 0.08);
      background: rgba(255,255,255,0.68);
    }}

    .graph-note-row span {{
      color: rgba(89, 59, 34, 0.62);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .graph-note-row strong {{
      color: var(--foreground);
      font-size: 13px;
      text-align: right;
    }}

    .graph-legend {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}

    .graph-legend-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 13px;
      color: var(--foreground);
    }}

    .graph-swatch {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
      flex-shrink: 0;
    }}

    .graph-swatch.external {{
      background: #eb6359;
    }}

    .graph-swatch.internal {{
      background: #312833;
    }}

    .graph-selection-empty {{
      color: var(--muted);
    }}

    .graph-selection-panel {{
      display: grid;
      gap: 10px;
    }}

    .graph-selection-kicker {{
      color: rgba(89, 59, 34, 0.62);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      line-height: 1.35;
    }}

    .graph-selection-head {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
    }}

    .graph-selection-head h4 {{
      margin: 0;
      font-size: 1.1rem;
      line-height: 1.18;
      letter-spacing: -0.03em;
    }}

    .graph-selection-score {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      background: rgba(66,43,29,0.92);
      color: #fff8eb;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}

    .graph-selection-rule {{
      margin: 0;
      color: rgba(64, 46, 34, 0.72);
      line-height: 1.58;
    }}

    .graph-selection-pill,
    .graph-fallback-badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      background: rgba(242, 196, 100, 0.18);
      color: rgba(66, 43, 29, 0.82);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.02em;
      border: 1px solid rgba(89, 59, 34, 0.08);
    }}

    .graph-selection-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}

    .graph-selection-link {{
      display: inline-flex;
      margin-top: 2px;
      color: var(--primary);
      font-size: 13px;
      font-weight: 700;
      line-height: 1.35;
    }}

    .graph-fallback {{
      display: grid;
      gap: 12px;
    }}

    .graph-fallback-group {{
      display: grid;
      gap: 10px;
    }}

    .graph-fallback-stack {{
      display: grid;
      gap: 8px;
    }}

    .graph-fallback-item {{
      padding: 12px;
      border-radius: 14px;
      border: 1px solid rgba(89,59,34,0.08);
      background: rgba(255,255,255,0.72);
    }}

    .graph-fallback-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}

    .graph-fallback-top strong,
    .graph-fallback-top a {{
      color: var(--foreground);
      font-size: 13px;
    }}

    .site-footer {{
      margin-top: 16px;
      padding: 10px 4px 0;
      text-align: center;
      color: var(--muted-soft);
      font-size: 12px;
      letter-spacing: 0.04em;
    }}

    .repo {{
      margin-top: 12px;
      font-size: 12px;
      color: rgba(31, 31, 37, 0.72);
    }}

    .repo a,
    .value a {{
      color: var(--primary);
    }}

    @media (max-width: 860px) {{
      .hero, .stats, .skill-grid, .graph-layout, .graph-detail-grid, .agent-pdp, .runtime-grid {{ grid-template-columns: 1fr; }}
      body {{ padding: 16px; }}
      .site-header {{
        padding: 12px 14px;
      }}
      .hero-shell {{ border-radius: 22px; padding: 12px; }}
      .card {{ padding: 15px; }}
      h1 {{ max-width: none; }}
      .hero-meta {{ margin-top: 12px; }}
      .stat {{ min-height: 0; }}
      .agent-card-shell {{ border-radius: 0; padding: 0; }}
      .agent-visual-panel {{
        border-right: 0;
        border-bottom: 1px solid rgba(31,31,37,0.08);
      }}
      .agent-pdp {{
        align-items: stretch;
      }}
      .graph-toolbar {{
        grid-template-columns: 1fr;
      }}
      .agent-detail-rail {{
        padding: 16px;
      }}
      .capability-copy {{
        flex-direction: column;
        align-items: start;
      }}
      .runtime-table-head {{
        flex-direction: column;
        align-items: start;
      }}
      .graph-head {{
        flex-direction: column;
        align-items: start;
      }}
      .graph-summary {{
        text-align: left;
      }}
      .graph-collector-top {{
        flex-direction: column;
      }}
      .graph-card-vitals {{
        justify-items: start;
      }}
    }}

    @media (max-width: 700px) {{
      body {{
        padding: 12px;
      }}
      .site-header {{
        align-items: center;
        padding: 10px 12px;
        margin-bottom: 10px;
      }}
      .brand-copy {{
        max-width: 12rem;
      }}
      .nav-cta {{
        min-height: 38px;
        padding: 0 12px;
        font-size: 12px;
      }}
      .graph-canvas-shell {{
        height: 16rem;
      }}
      .agent-surface {{
        margin-top: 10px;
      }}
      .agent-pdp {{
        border-radius: 22px;
      }}
      .agent-visual-panel {{
        padding: 10px;
      }}
      .graph-toolbar,
      .graph-toolbar-meta,
      .graph-selection-dock {{
        display: none;
      }}
      .agent-visual-panel .graph-canvas-shell {{
        height: min(48vh, 16rem);
        border-radius: 18px;
      }}
      .agent-detail-rail {{
        padding: 14px;
        gap: 12px;
      }}
      .agent-detail-title h1 {{
        font-size: clamp(1.9rem, 10vw, 2.7rem);
      }}
      .agent-detail-summary {{
        padding-top: 10px;
        font-size: 0.9rem;
      }}
      .agent-cta {{
        min-height: 48px;
      }}
      .agent-detail-row {{
        grid-template-columns: 1fr;
        padding: 10px 0;
      }}
      .agent-detail-row strong,
      .agent-detail-row a {{
        text-align: left;
      }}
      .runtime-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class='wrap'>
    <header class='site-header'>
      <a class='brand' href='{radius_site}' target='_blank' rel='noopener'>
        <svg class='brand-mark' viewBox='0 0 111 78' fill='none' xmlns='http://www.w3.org/2000/svg' aria-hidden='true'>
          <path d='M55.4453 3.95455C69.815 -3.13177 87.7166 -0.694272 99.6572 11.2661C106.923 18.5439 110.916 28.2165 110.916 38.5053C110.916 48.7943 106.911 58.4676 99.6572 65.7456C92.4029 73.0235 82.3162 77.0112 72.4619 77.0112C66.6326 77.0112 60.808 75.696 55.4697 73.0678C50.1251 75.7038 44.292 77.0229 38.4541 77.0229V77.0112C28.5998 77.0112 18.7569 73.2561 11.2588 65.7456C5.04447 59.5209 1.21728 51.5301 0.24707 42.8989H0V33.9936H0.262695C1.25884 25.4167 5.07865 17.4781 11.2588 11.2778C23.1925 -0.675684 41.0799 -3.11677 55.4453 3.95455ZM38.4541 8.91744C30.8864 8.91744 23.3068 11.8005 17.5498 17.5786C13.0464 22.0894 10.1758 27.8034 9.2334 33.9936H34.0088V42.8989H9.21094C10.1307 49.144 13.0119 54.9111 17.5498 59.4565C25.3924 67.3119 36.5822 69.8188 46.5645 66.9838C46.1252 66.5826 45.6912 66.1709 45.2666 65.7456C30.2707 50.7246 30.2707 26.287 45.2666 11.2661C45.6859 10.846 46.1133 10.438 46.5469 10.0415C43.9052 9.2928 41.1798 8.91747 38.4541 8.91744ZM76.6387 42.9702C75.6788 51.3024 72.017 59.379 65.6494 65.7573C65.2278 66.1796 64.7964 66.5882 64.3604 66.9868C74.3404 69.8176 85.526 67.3096 93.3662 59.4565C97.8869 54.9284 100.763 49.1878 101.694 42.9702H76.6387ZM55.4551 14.2895C54.0856 15.2556 52.7803 16.3514 51.5576 17.5786C45.9748 23.1707 42.8985 30.6114 42.8984 38.5171C42.8984 46.4228 45.9747 53.8643 51.5576 59.4565C52.7813 60.6822 54.0876 61.7762 55.457 62.7417C56.8269 61.776 58.1344 60.6826 59.3584 59.4565C64.9413 53.8643 68.0166 46.4228 68.0166 38.5171C68.0165 30.6115 64.9412 23.1706 59.3584 17.5786C58.1331 16.3512 56.8255 15.2557 55.4551 14.2895ZM72.4619 8.91744C69.7328 8.91744 67.0026 9.29391 64.3564 10.0444C64.7939 10.4441 65.2265 10.8542 65.6494 11.2778C72.017 17.6561 75.6788 25.7327 76.6387 34.0649H101.694C100.763 27.8472 97.8869 22.1067 93.3662 17.5786C87.7833 11.9864 80.0296 8.91748 72.4619 8.91744Z' fill='currentColor'/>
        </svg>
        <span class='brand-copy'>
          <span class='brand-name'>Radius</span>
          <span class='brand-subtitle'>Agent Profile</span>
        </span>
      </a>
      <nav class='site-nav' aria-label='Agent actions'>
        <a class='nav-cta' href='{template_repo}' target='_blank' rel='noopener'>Create your agent</a>
      </nav>
    </header>

    <main class='agent-surface'>
      <section class='agent-card-shell'>
        <div class='agent-pdp'>
          <section class='agent-visual-panel'>
            <div
              class='graph-canvas-shell'
              id='agent-graph-root'
              data-graph-endpoint='/agent-graph.json'
            >
              <canvas class='graph-canvas' aria-label='Agent capability graph'></canvas>
              <div class='graph-label-layer' aria-hidden='true'></div>
              <div class='graph-status' data-graph-status hidden></div>
            </div>
            <div class='graph-toolbar'>
              <input
                class='graph-search-input'
                type='search'
                placeholder='Search skills, capabilities, tools, channels or surfaces'
                aria-label='Search graph nodes'
                data-graph-search
              >
              <select class='graph-filter-select' aria-label='Filter graph by type' data-graph-kind-filter>
                <option value='all'>All types</option>
                <option value='surface'>Surfaces</option>
                <option value='capability'>Capabilities</option>
                <option value='skill'>Skills</option>
                <option value='tool'>Tools</option>
                <option value='channel'>Channels</option>
              </select>
              <button class='graph-filter-reset' type='button' data-graph-clear>Reset</button>
            </div>
            <div class='graph-toolbar-meta'>
              <span class='graph-result-count' data-graph-result-count>All runtime nodes</span>
              <span class='graph-toolbar-hint'>Click a node to focus</span>
            </div>
            <section class='graph-selection-dock'>
              <div class='agent-detail-card'>
                <h2>Selected Node</h2>
                <p>Click a node in the graph to inspect its current role in the runtime.</p>
                <div data-graph-selection>
                  <p class='graph-selection-empty'>Select a node to inspect its current details.</p>
                </div>
              </div>
            </section>
          </section>

          <aside class='agent-detail-rail'>
            <div class='agent-detail-title'>
              <h1>{html.escape(agent_name)}</h1>
              <p>{html.escape(agent_description)}</p>
            </div>

            <div class='agent-detail-summary'>{html.escape(graph_summary)}</div>

            {wallet_note}

            <div class='agent-detail-table'>
              <div class='agent-detail-row'><span>SBC Balance</span><strong>{html.escape(sbc_balance)}</strong></div>
              <div class='agent-detail-row'><span>RUSD Balance</span><strong>{html.escape(rusd_balance)}</strong></div>
              <div class='agent-detail-row'><span>DID</span><strong>{html.escape(did)}</strong></div>
              <div class='agent-detail-row'><span>EVM Address</span><a href='{html.escape(explorer_link)}' target='_blank' rel='noopener'>{html.escape(wallet_address)}</a></div>
              <div class='agent-detail-row'><span>Discovery</span><strong>{graph_external_count}</strong></div>
              <div class='agent-detail-row'><span>Internals</span><strong>{graph_internal_count}</strong></div>
              <div class='agent-detail-row'><span>Capabilities</span><strong>{graph_capability_count}</strong></div>
              <div class='agent-detail-row'><span>Tools</span><strong>{graph_tool_count}</strong></div>
              <div class='agent-detail-row'><span>Published Skills</span><strong>{graph_published_skill_count}</strong></div>
            </div>
          </aside>
        </div>
      </section>
      <section class='agent-runtime-sections'>
        <div class='agent-runtime-head'>
          <h2>Runtime Details</h2>
          <p>Capabilities, tools, skills, channels, and external surfaces generated from the current implementation.</p>
        </div>
        <section class='runtime-table-section'>
          <div class='runtime-table-head'>
            <h3>Capabilities</h3>
            <p>Plugin-backed and runtime-native capabilities, with the tools summarized in each row.</p>
          </div>
          {capability_table_html}
        </section>
        <section class='runtime-table-section'>
          <div class='runtime-table-head'>
            <h3>External Surfaces & Interfaces</h3>
            <p>Well-known discovery URIs and public operational interfaces exposed by this agent.</p>
          </div>
          {surface_table_html}
        </section>
      </section>
    </main>

  </div>
  <script type='importmap'>{import_map_json}</script>
  <script type='module' src='/static/js/homepage.js'></script>
</body>
</html>"""
    )
    response.headers["Link"] = _homepage_link_header()
    return response


@app.api_route("/.well-known/api-catalog", methods=["GET", "HEAD"])
async def api_catalog(request: Request):
    docs_url = f"{BASE_URL}/"
    openapi_url = f"{BASE_URL}/openapi.json"
    status_url = f"{BASE_URL}/health"

    def links(*items: tuple[str, str]) -> list[dict[str, str]]:
        return [{"rel": rel, "href": href} for rel, href in items]

    payload = {
        "linkset": [
            {
                "anchor": f"{BASE_URL}/a2a",
                "links": links(
                    ("service-desc", f"{BASE_URL}/.well-known/agent-card.json"),
                    ("service-doc", docs_url),
                    ("status", status_url),
                ),
            },
            {
                "anchor": f"{BASE_URL}/.well-known/agent-card.json",
                "links": links(
                    ("service-desc", f"{BASE_URL}/.well-known/agent-card.json"),
                    ("service-doc", docs_url),
                    ("status", status_url),
                ),
            },
            {
                "anchor": f"{BASE_URL}/.well-known/agent-skills/index.json",
                "links": links(
                    ("service-desc", f"{BASE_URL}/.well-known/agent-skills/index.json"),
                    ("service-doc", docs_url),
                    ("status", status_url),
                ),
            },
            {
                "anchor": openapi_url,
                "links": links(
                    ("service-desc", openapi_url),
                    ("service-doc", docs_url),
                    ("status", status_url),
                ),
            },
        ]
    }
    headers = {"Cache-Control": "public, max-age=300"}
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/linkset+json", headers=headers)
    return Response(
        content=json.dumps(payload),
        media_type="application/linkset+json",
        headers=headers,
    )


@app.get("/.well-known/openid-configuration")
async def openid_configuration():
    issuer = os.environ.get("OAUTH_ISSUER", BASE_URL)
    authorization_endpoint = os.environ.get(
        "OAUTH_AUTHORIZATION_ENDPOINT", f"{issuer}/oauth/authorize"
    )
    token_endpoint = os.environ.get("OAUTH_TOKEN_ENDPOINT", f"{issuer}/token")
    jwks_uri = os.environ.get("OAUTH_JWKS_URI", f"{issuer}/.well-known/jwks.json")
    grant_types_supported = os.environ.get(
        "OAUTH_GRANT_TYPES_SUPPORTED", "client_credentials"
    ).split(",")
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": token_endpoint,
            "jwks_uri": jwks_uri,
            "grant_types_supported": [item.strip() for item in grant_types_supported if item.strip()],
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    issuer = os.environ.get("OAUTH_ISSUER", BASE_URL)
    token_endpoint = os.environ.get("OAUTH_TOKEN_ENDPOINT", f"{issuer}/token")
    jwks_uri = os.environ.get("OAUTH_JWKS_URI", f"{issuer}/.well-known/jwks.json")
    grant_types_supported = os.environ.get(
        "OAUTH_GRANT_TYPES_SUPPORTED", "client_credentials"
    ).split(",")
    return JSONResponse(
        {
            "issuer": issuer,
            "token_endpoint": token_endpoint,
            "jwks_uri": jwks_uri,
            "grant_types_supported": [item.strip() for item in grant_types_supported if item.strip()],
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    resource = os.environ.get("OAUTH_RESOURCE", BASE_URL)
    authorization_servers = os.environ.get("OAUTH_AUTHORIZATION_SERVERS", BASE_URL).split(",")
    scopes_supported = os.environ.get(
        "OAUTH_SCOPES_SUPPORTED", "a2a.read a2a.write health.read files.read"
    ).split()
    return JSONResponse(
        {
            "resource": resource,
            "authorization_servers": [
                server.strip() for server in authorization_servers if server.strip()
            ],
            "scopes_supported": [scope.strip() for scope in scopes_supported if scope.strip()],
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


async def _handle_delegated(rpc_id, message: dict, issuer_did: str | None):
    webhook_secret = os.environ.get("WEBHOOK_SECRET")
    if not webhook_secret:
        return _rpc_error_response(
            rpc_id,
            InternalError(message="Webhook not configured on this agent"),
            status_code=503,
        )

    parts = message.get("parts") or []
    text = "\n".join(p["text"] for p in parts if isinstance(p.get("text"), str)).strip()
    if not text:
        return _rpc_error_response(
            rpc_id,
            InvalidParamsError(
                message="Invalid params: no text content in message parts"
            ),
        )

    task_id = str(uuid.uuid4())
    context_id = message.get("context_id", task_id)
    update_request_context(
        rpc_id=rpc_id,
        context_id=context_id,
        issuer_did=issuer_did,
        a2a_mode="delegated",
        a2a_task_id=task_id,
    )
    issuer_did_url = _did_web_to_base_url(issuer_did) if issuer_did else None
    session = _a2a_session_store.find_by_context(context_id)
    webhook_payload = json.dumps(
        {
            "text": text,
            "context_id": context_id,
            "task_id": task_id,
            **({"issuer_did": issuer_did} if issuer_did else {}),
            **({"issuer_did_url": issuer_did_url} if issuer_did_url else {}),
            **({"a2a_session_id": session.get("session_id")} if session else {}),
            **(
                {"a2a_session_goal": session.get("goal") or session.get("topic")}
                if session
                else {}
            ),
            **(
                {"a2a_session_turn_count": session.get("turn_count")} if session else {}
            ),
            **(
                {"a2a_session_auto_continue": session.get("auto_continue")}
                if session
                else {}
            ),
        }
    )
    sig = hmac.new(
        webhook_secret.encode("utf-8"), webhook_payload.encode("utf-8"), "sha256"
    ).hexdigest()
    webhook_port = os.environ.get("WEBHOOK_PORT", "8644")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            webhook_res = await client.post(
                f"http://localhost:{webhook_port}/webhooks/a2a",
                content=webhook_payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Signature": sig,
                },
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
                InternalError(
                    message=f"Webhook delivery failed: HTTP {webhook_res.status_code}"
                ),
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
            InternalError(
                message="Could not reach agent backend — ensure WEBHOOK_ENABLED=true and WEBHOOK_SECRET is set"
            ),
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
        {
            "id": task_id,
            "context_id": context_id,
            "status": {
                "state": "TASK_STATE_SUBMITTED",
                "timestamp_ms": int(time.time() * 1000),
            },
        },
    )


def _hermes_error_response(rpc_id, exc: Exception) -> JSONResponse:
    if isinstance(exc, HermesUnavailableError):
        log_event(
            logger,
            logging.WARNING,
            "Direct A2A cannot reach Hermes backend",
            event="a2a.direct",
            outcome="error",
            rpc_id=rpc_id,
            hermes_error=str(exc),
            error_type="unavailable",
        )
        return _rpc_error_response(
            rpc_id,
            InternalError(
                message="Hermes backend is unreachable. Check HERMES_URL and HERMES_API_KEY/API_SERVER_KEY."
            ),
            status_code=503,
        )
    if isinstance(exc, HermesUpstreamError):
        log_event(
            logger,
            logging.WARNING,
            "Direct A2A Hermes upstream returned an error",
            event="a2a.direct",
            outcome="error",
            rpc_id=rpc_id,
            hermes_error=str(exc),
            error_type="upstream",
        )
        return _rpc_error_response(
            rpc_id,
            InternalError(message=str(exc)),
            status_code=502,
        )
    log_event(
        logger,
        logging.ERROR,
        "Direct A2A failure",
        event="a2a.direct",
        outcome="error",
        rpc_id=rpc_id,
        error_type=type(exc).__name__,
        exc_info=True,
    )
    return _rpc_error_response(
        rpc_id, InternalError(message="Internal processing error")
    )


@app.post("/a2a")
async def handle_a2a(request: Request, auth: dict = Depends(jwt_auth_dep)):
    try:
        body = await request.json()
    except Exception:
        log_event(
            logger,
            logging.WARNING,
            "A2A request body could not be parsed",
            event="a2a.request",
            outcome="rejected",
            rejection_reason="json_parse_error",
        )
        return _rpc_error_response(
            None, JSONParseError(message="Parse error"), status_code=400
        )

    try:
        parsed_request = JSONRPCRequest.model_validate(body)
        body = parsed_request.model_dump(by_alias=True, exclude_none=True)
    except Exception:
        log_event(
            logger,
            logging.WARNING,
            "A2A request failed schema validation",
            event="a2a.request",
            outcome="rejected",
            rejection_reason="invalid_request",
        )
        return _rpc_error_response(
            body.get("id") if isinstance(body, dict) else None,
            InvalidRequestError(),
            status_code=400,
        )

    if body.get("jsonrpc") != "2.0" or not body.get("method"):
        log_event(
            logger,
            logging.WARNING,
            "A2A request missing required JSON-RPC fields",
            event="a2a.request",
            outcome="rejected",
            rejection_reason="invalid_jsonrpc_envelope",
            rpc_id=body.get("id"),
        )
        return _rpc_error_response(
            body.get("id"),
            InvalidRequestError(message="Invalid Request"),
            status_code=400,
        )

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    message = params.get("message")
    if not message:
        log_event(
            logger,
            logging.WARNING,
            "A2A request missing message payload",
            event="a2a.request",
            outcome="rejected",
            rejection_reason="missing_message",
            rpc_id=rpc_id,
            rpc_method=method,
        )
        return _rpc_error_response(
            rpc_id, InvalidParamsError(message="Invalid params: missing message")
        )

    mode = _resolve_mode(method)
    message_id = message.get("message_id") if isinstance(message, dict) else None
    if isinstance(message, dict) and not message_id:
        message_id = message.get("id")
    context_id = message.get("context_id") if isinstance(message, dict) else None
    prompt_chars = 0
    if isinstance(message, dict):
        prompt_chars = sum(
            len(part.get("text", ""))
            for part in (message.get("parts") or [])
            if isinstance(part.get("text"), str)
        )
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
    managed_session = (
        _a2a_session_store.find_by_context(context_id) if context_id else None
    )
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
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "timestamp_ms": int(time.time() * 1000),
            },
            "message": {
                "role": "agent",
                "context_id": context_id,
                "parts": [
                    {
                        "type": "text",
                        "text": "Turn received. Continuing the managed A2A session.",
                    }
                ],
            },
        }
        return _rpc_success_response(rpc_id, result)
    if method not in {"message/send", "message/stream"}:
        log_event(
            logger,
            logging.WARNING,
            "A2A method not supported",
            event="a2a.request",
            outcome="rejected",
            rejection_reason="method_not_supported",
            rpc_id=rpc_id,
            rpc_method=method,
        )
        return _rpc_error_response(
            rpc_id, MethodNotFoundError(message="This operation is not supported")
        )

    if mode == "delegated":
        if method == "message/stream":
            log_event(
                logger,
                logging.WARNING,
                "A2A streaming requires direct mode",
                event="a2a.request",
                outcome="rejected",
                rejection_reason="stream_requires_direct_mode",
                rpc_id=rpc_id,
                rpc_method=method,
            )
            return _rpc_error_response(
                rpc_id,
                MethodNotFoundError(
                    message="message/stream is only supported in direct mode"
                ),
            )
        return await _handle_delegated(rpc_id, message, auth.get("issuer"))

    if not _a2a_bridge:
        log_event(
            logger,
            logging.ERROR,
            "Direct A2A bridge unavailable",
            event="a2a.direct",
            outcome="error",
            rpc_id=rpc_id,
            rpc_method=method,
        )
        return _rpc_error_response(
            rpc_id,
            InternalError(message="Direct A2A bridge is unavailable"),
            status_code=503,
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
                    log_event(
                        logger,
                        logging.WARNING,
                        "Direct A2A streaming cannot reach Hermes backend",
                        event="a2a.direct_stream",
                        outcome="error",
                        rpc_id=rpc_id,
                        issuer_did=auth.get("issuer"),
                        context_id=get_request_context().get("context_id"),
                        hermes_error=str(exc),
                        error_type="unavailable",
                    )
                    message_text = "Hermes backend is unreachable. Check HERMES_URL and HERMES_API_KEY/API_SERVER_KEY."
                else:
                    log_event(
                        logger,
                        logging.WARNING,
                        "Direct A2A streaming Hermes upstream returned an error",
                        event="a2a.direct_stream",
                        outcome="error",
                        rpc_id=rpc_id,
                        issuer_did=auth.get("issuer"),
                        context_id=get_request_context().get("context_id"),
                        hermes_error=str(exc),
                        error_type="upstream",
                    )
                    message_text = str(exc)
                err = {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": _jsonrpc_error_payload(InternalError(message=message_text)),
                }
                yield f"data: {json.dumps(err)}\n\n"
            except Exception:
                log_event(
                    logger,
                    logging.ERROR,
                    "Direct A2A streaming failure",
                    event="a2a.direct_stream",
                    outcome="error",
                    rpc_id=rpc_id,
                    issuer_did=auth.get("issuer"),
                    context_id=get_request_context().get("context_id"),
                    exc_info=True,
                )
                err = {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": _jsonrpc_error_payload(
                        InternalError(message="Internal processing error")
                    ),
                }
                yield f"data: {json.dumps(err)}\n\n"
            finally:
                clear_request_context(stream_token)

        return StreamingResponse(_sse(), media_type="text/event-stream")
    except ValueError:
        log_event(
            logger,
            logging.WARNING,
            "A2A request failed parameter validation",
            event="a2a.request",
            outcome="rejected",
            rejection_reason="invalid_params",
            rpc_id=rpc_id,
            rpc_method=method,
        )
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
            return Response(
                content=candidate.read_bytes(), media_type="application/octet-stream"
            )
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
        log_event(
            logger,
            logging.WARNING,
            "Token exchange rejected",
            event="token.exchange",
            outcome="unauthorized",
        )
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    sub = "client"
    try:
        body = await request.json()
        if isinstance(body.get("sub"), str):
            sub = body["sub"]
    except Exception:
        pass
    token = await issue_token(sub)
    log_event(
        logger,
        logging.INFO,
        "Token issued",
        event="token.exchange",
        outcome="issued",
        token_subject=sub,
    )
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
