"""Compatibility boundary for the external A2A SDK.

The A2A SDK has moved error helpers between releases. Keep direct imports from
the SDK in this module so server startup does not depend on private helper paths.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _FallbackRPCError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: int
    data: Any | None = None
    message: str | None = None


try:
    from a2a.types import (  # type: ignore
        InternalError,
        InvalidParamsError,
        InvalidRequestError,
        JSONParseError,
        MethodNotFoundError,
    )
except Exception:

    class InternalError(_FallbackRPCError):
        code: int = -32603
        message: str | None = "Internal error"

    class InvalidParamsError(_FallbackRPCError):
        code: int = -32602
        message: str | None = "Invalid parameters"

    class InvalidRequestError(_FallbackRPCError):
        code: int = -32600
        message: str | None = "Request payload validation error"

    class JSONParseError(_FallbackRPCError):
        code: int = -32700
        message: str | None = "Invalid JSON payload"

    class MethodNotFoundError(_FallbackRPCError):
        code: int = -32601
        message: str | None = "Method not found"


_DEFAULT_ERROR_CODES: dict[type, int] = {
    InternalError: -32603,
    InvalidParamsError: -32602,
    InvalidRequestError: -32600,
    JSONParseError: -32700,
    MethodNotFoundError: -32601,
}


def _model_payload(error_obj: Any) -> dict[str, Any] | None:
    if not hasattr(error_obj, "model_dump"):
        return None
    payload = error_obj.model_dump(by_alias=True, exclude_none=True)
    if isinstance(payload, dict):
        return payload
    return None


def _error_code(error_obj: Any, payload: dict[str, Any]) -> int:
    code = payload.get("code", getattr(error_obj, "code", None))
    try:
        return int(code)
    except (TypeError, ValueError):
        return _DEFAULT_ERROR_CODES.get(type(error_obj), -32603)


def jsonrpc_error_payload(error_obj: Any) -> dict[str, Any]:
    """Return a stable JSON-RPC error dict for SDK or local error objects."""
    root = getattr(error_obj, "root", None)
    if root is not None:
        error_obj = root

    payload = _model_payload(error_obj) or {}
    code = _error_code(error_obj, payload)
    message = payload.get("message", getattr(error_obj, "message", None))
    if not message:
        message = str(error_obj) or "Internal error"

    normalized: dict[str, Any] = {
        "code": code,
        "message": str(message),
    }
    data = payload.get("data", getattr(error_obj, "data", None))
    if data is not None:
        normalized["data"] = data
    return normalized
