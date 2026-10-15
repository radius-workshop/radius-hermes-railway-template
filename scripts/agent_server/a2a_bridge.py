"""Direct A2A bridge for translating A2A JSON-RPC messages to Hermes chat calls."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

from file_utils import allowed_file_paths, detect_file_paths, infer_mime_type, replace_file_paths
from hermes_client import HermesClient


class A2ABridge:
    def __init__(self, hermes_client: HermesClient, allowed_roots: list[Path], public_base_url: str):
        self.hermes_client = hermes_client
        self.allowed_roots = [root.resolve() for root in allowed_roots if root.exists()]
        self.public_base_url = public_base_url.rstrip("/")

    @staticmethod
    def extract_text(message: dict) -> str:
        parts = message.get("parts") or []
        return "\n".join(part.get("text", "") for part in parts if isinstance(part.get("text"), str)).strip()

    @staticmethod
    def context_id(message: dict) -> str:
        return message.get("context_id") or str(uuid.uuid4())

    def _attachments_from_text(self, text: str) -> tuple[str, list[dict]]:
        raw_paths = detect_file_paths(text)
        files = allowed_file_paths(raw_paths, self.allowed_roots)
        if not files:
            return text, []

        replacements: dict[str, str] = {}
        attachments: list[dict] = []
        for file_path in files:
            rel_url = self._to_public_file_path(file_path)
            if not rel_url:
                continue
            display = f"[file: {file_path.name}]"
            replacements[str(file_path)] = display
            attachments.append(
                {
                    "type": "file",
                    "name": file_path.name,
                    "mime_type": infer_mime_type(str(file_path)),
                    "size_bytes": file_path.stat().st_size,
                    "url": rel_url,
                }
            )
        return replace_file_paths(text, replacements), attachments

    def _to_public_file_path(self, file_path: Path) -> str | None:
        for root in self.allowed_roots:
            try:
                rel = file_path.relative_to(root)
                return f"{self.public_base_url}/files/{quote(str(rel))}"
            except ValueError:
                continue
        return None

    async def handle_send(self, rpc_id, message: dict) -> dict:
        user_text = self.extract_text(message)
        if not user_text:
            raise ValueError("Invalid params: no text content in message parts")

        context_id = self.context_id(message)
        task_id = str(uuid.uuid4())
        output = await self.hermes_client.complete(
            messages=[{"role": "user", "content": user_text}],
            session_id=context_id,
        )
        rendered, attachments = self._attachments_from_text(output)
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "id": task_id,
                "context_id": context_id,
                "status": {"state": "TASK_STATE_COMPLETED", "timestamp_ms": int(time.time() * 1000)},
                "message": {
                    "role": "agent",
                    "context_id": context_id,
                    "parts": [{"type": "text", "text": rendered}],
                    **({"attachments": attachments} if attachments else {}),
                },
            },
        }

    async def stream_events(self, rpc_id, message: dict) -> AsyncIterator[dict]:
        user_text = self.extract_text(message)
        if not user_text:
            raise ValueError("Invalid params: no text content in message parts")

        context_id = self.context_id(message)
        full_text = ""
        async for chunk in self.hermes_client.stream(
            messages=[{"role": "user", "content": user_text}],
            session_id=context_id,
        ):
            full_text += chunk
            yield {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "type": "message.delta",
                    "context_id": context_id,
                    "delta": chunk,
                },
            }

        rendered, attachments = self._attachments_from_text(full_text)
        yield {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "type": "message.completed",
                "context_id": context_id,
                "message": {
                    "role": "agent",
                    "context_id": context_id,
                    "parts": [{"type": "text", "text": rendered}],
                    **({"attachments": attachments} if attachments else {}),
                },
                "status": {"state": "TASK_STATE_COMPLETED", "timestamp_ms": int(time.time() * 1000)},
            },
        }
