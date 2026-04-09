"""Async Hermes OpenAI-compatible chat client."""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx


class HermesClientError(Exception):
    """Base exception for Hermes client errors."""


class HermesUnavailableError(HermesClientError):
    """Raised when Hermes is unreachable (network/timeout)."""


class HermesUpstreamError(HermesClientError):
    """Raised when Hermes returns an HTTP error."""


class HermesClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    def _headers(self, session_id: Optional[str] = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        return headers

    async def complete(self, messages: list[dict], session_id: Optional[str] = None) -> str:
        payload = {"model": self.model, "messages": messages, "stream": False}
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=self._headers(session_id),
                json=payload,
            )
            response.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise HermesUnavailableError(f"Unable to reach Hermes at {self.base_url}") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else "unknown"
            raise HermesUpstreamError(f"Hermes returned HTTP {status}") from exc
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return message.get("content") or ""

    async def stream(self, messages: list[dict], session_id: Optional[str] = None) -> AsyncIterator[str]:
        payload = {"model": self.model, "messages": messages, "stream": True}
        try:
            async with self._client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                headers=self._headers(session_id),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for choice in event.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield delta
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise HermesUnavailableError(f"Unable to reach Hermes at {self.base_url}") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else "unknown"
            raise HermesUpstreamError(f"Hermes returned HTTP {status}") from exc

    async def close(self):
        await self._client.aclose()
