import json
import os
import re
from contextvars import ContextVar
from pathlib import Path


VALID_PROVIDERS = {"local", "para"}
WALLET_TOOL_NAMES = {"radius_wallet_address", "radius_balance", "radius_send_sbc"}
_CURRENT_TASK_ID: ContextVar[str] = ContextVar("radius_cast_task_id", default="")


def _radius_root() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    path = Path(hermes_home) / ".radius"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _provider_session_root() -> Path:
    path = _radius_root() / "wallet-provider-sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_provider(value: str | None) -> str:
    provider = str(value or "").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise ValueError(
            f"Invalid wallet provider {value!r}. Expected one of: {', '.join(sorted(VALID_PROVIDERS))}."
        )
    return provider


def _normalize_session_id(session_id: str | None) -> str:
    value = str(session_id or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value):
        raise ValueError("Invalid session_id")
    return value


def _session_path(session_id: str) -> Path:
    return (_provider_session_root() / f"{_normalize_session_id(session_id)}.json").resolve()


def get_session_provider(session_id: str | None) -> str:
    normalized = _normalize_session_id(session_id)
    if not normalized:
        return "local"
    path = _session_path(normalized)
    if not path.exists():
        return "local"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "local"
    provider = str(data.get("provider") or "").strip().lower()
    return provider if provider in VALID_PROVIDERS else "local"


def get_session_record(session_id: str | None) -> dict:
    normalized = _normalize_session_id(session_id)
    if not normalized:
        return {"session_id": "", "provider": "local"}
    path = _session_path(normalized)
    if not path.exists():
        return {"session_id": normalized, "provider": "local"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"session_id": normalized, "provider": "local"}
    provider = str(data.get("provider") or "").strip().lower()
    if provider not in VALID_PROVIDERS:
        provider = "local"
    record = {"session_id": normalized, "provider": provider}
    error = str(data.get("error") or "").strip()
    if error:
        record["error"] = error
    return record


def set_session_provider(session_id: str, provider: str) -> str:
    normalized_session_id = _normalize_session_id(session_id)
    normalized_provider = normalize_provider(provider)
    path = _session_path(normalized_session_id)
    payload = {"session_id": normalized_session_id, "provider": normalized_provider}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return normalized_provider


def set_session_provider_error(session_id: str, provider: str, error: str) -> str:
    normalized_session_id = _normalize_session_id(session_id)
    normalized_provider = normalize_provider(provider)
    path = _session_path(normalized_session_id)
    payload = {
        "session_id": normalized_session_id,
        "provider": normalized_provider,
        "error": str(error or "").strip(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return normalized_provider


def resolve_provider(explicit_provider: str | None = None, session_id: str | None = None) -> str:
    if explicit_provider not in (None, ""):
        return normalize_provider(explicit_provider)
    record = get_session_record(session_id)
    if record.get("provider") == "para" and record.get("error"):
        raise RuntimeError(record["error"])
    return str(record.get("provider") or "local")


def current_task_id() -> str:
    return str(_CURRENT_TASK_ID.get() or "").strip()


def current_session_id(kwargs: dict | None = None) -> str:
    kwargs = kwargs or {}
    for key in ("task_id", "session_id"):
        value = str(kwargs.get(key) or "").strip()
        if value:
            return value
    return current_task_id()


def pre_tool_call(tool_name: str, args: dict, task_id: str, **kwargs):
    if tool_name in WALLET_TOOL_NAMES:
        _CURRENT_TASK_ID.set(str(task_id or "").strip())


def post_tool_call(tool_name: str, args: dict, result: str, task_id: str, **kwargs):
    if tool_name in WALLET_TOOL_NAMES:
        _CURRENT_TASK_ID.set("")
