"""Durable session store for long-running A2A conversations."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from a2a_render import build_status_card, build_turn_card


class A2ASessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._remote_api_keys: dict[str, str] = {}

    def create_or_update_outbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self._ts()
        session_id = self._normalize_session_id(
            str(payload.get("session_id") or "").strip() or str(uuid.uuid4())
        )
        session = self.get_session(session_id) or self._new_session(session_id, now)

        context_id = str(payload.get("context_id") or session.get("context_id") or "").strip()
        if context_id:
            session["context_id"] = context_id

        remote_agent = str(payload.get("remote_agent") or session.get("remote_agent") or "").strip()
        if remote_agent:
            session["remote_agent"] = remote_agent

        remote_did = str(payload.get("remote_did") or session.get("remote_did") or "").strip()
        if remote_did:
            session["remote_did"] = remote_did

        remote_api_key = str(payload.get("remote_api_key") or "").strip()
        if remote_api_key:
            self._remote_api_keys[session_id] = remote_api_key
        session.pop("remote_api_key", None)

        goal = str(payload.get("goal") or session.get("goal") or "").strip()
        if goal:
            session["goal"] = goal

        topic = str(payload.get("topic") or session.get("topic") or "").strip()
        if topic:
            session["topic"] = topic

        if "max_turns" in payload:
            max_turns = payload.get("max_turns")
            session["max_turns"] = int(max_turns) if max_turns not in (None, "") else None

        if "auto_continue" in payload:
            auto_continue = bool(payload.get("auto_continue"))
        else:
            auto_continue = bool(session.get("auto_continue", False))
        session["auto_continue"] = auto_continue
        session["controller_mode"] = "local" if auto_continue else session.get("controller_mode") or "none"

        origin = payload.get("origin")
        if isinstance(origin, dict):
            merged_origin = dict(session.get("origin") or {})
            merged_origin.update({k: v for k, v in origin.items() if v not in (None, "")})
            session["origin"] = merged_origin

        if payload.get("message_id"):
            session["last_outbound_message_id"] = str(payload["message_id"])

        task_text = str(payload.get("task") or "").strip()
        if task_text:
            self._append_event(
                session,
                {
                    "event": "outbound.submit",
                    "speaker": "local",
                    "message_id": payload.get("message_id"),
                    "context_id": context_id,
                    "text": task_text,
                },
            )
            self._append_transcript(session, "local", task_text)
            session["last_speaker"] = "local"
            session["waiting_on"] = "remote"
            session["next_action"] = "await_remote"
            self._set_latest_card(session, build_turn_card(session, "You", task_text))

        session["status"] = session.get("status") or "active"
        session["updated_at"] = now
        self._save_session(session)
        return session

    def record_outbound_result(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        session_id = str(payload.get("session_id") or "").strip()
        context_id = str(payload.get("context_id") or "").strip()
        session = self.get_session(session_id) if session_id else None
        if not session and context_id:
            session = self.find_by_context(context_id)
        if not session:
            return None

        now = self._ts()
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        status = result.get("status") if isinstance(result.get("status"), dict) else {}
        task_state = status.get("state") or payload.get("task_state")
        remote_text = self._extract_message_text(result.get("message"))

        event = {
            "event": "outbound.result",
            "speaker": "remote" if remote_text else "system",
            "context_id": result.get("context_id") or context_id or session.get("context_id"),
            "a2a_task_id": result.get("id") or payload.get("a2a_task_id"),
            "task_state": task_state,
            "duration_ms": payload.get("duration_ms"),
            "text": remote_text,
        }
        self._append_event(session, event)

        resolved_context = str(event["context_id"] or "").strip()
        if resolved_context:
            session["context_id"] = resolved_context

        if remote_text:
            self._append_transcript(session, "remote", remote_text)
            session["turn_count"] = int(session.get("turn_count") or 0) + 1
            session["last_speaker"] = "remote"
            if session.get("auto_continue") and not self._stop_due_to_limit(session):
                session["waiting_on"] = "local"
                session["next_action"] = "compose_local_turn"
                session["next_action_at"] = time.time()
            else:
                session["waiting_on"] = None
                session["next_action"] = "idle"
            self._set_latest_card(session, build_turn_card(session, "Peer Agent", remote_text))
        else:
            session["waiting_on"] = "remote"
            session["next_action"] = "await_remote"
            self._set_latest_status_card(session)

        session["last_remote_state"] = task_state
        session["updated_at"] = now
        session["status"] = "completed" if self._stop_due_to_limit(session) else "active"
        self._save_session(session)
        return session

    def record_inbound_message(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        context_id = str(payload.get("context_id") or "").strip()
        if not context_id:
            return None
        session = self.find_by_context(context_id)
        if not session:
            return None

        now = self._ts()
        message_text = str(payload.get("text") or "").strip()
        if message_text:
            self._append_event(
                session,
                {
                    "event": "inbound.remote_message",
                    "speaker": "remote",
                    "context_id": context_id,
                    "issuer_did": payload.get("issuer_did"),
                    "text": message_text,
                },
            )
            self._append_transcript(session, "remote", message_text)
            session["turn_count"] = int(session.get("turn_count") or 0) + 1
            session["last_speaker"] = "remote"
            if session.get("auto_continue") and not self._stop_due_to_limit(session):
                session["waiting_on"] = "local"
                session["next_action"] = "compose_local_turn"
                session["next_action_at"] = time.time()
            else:
                session["waiting_on"] = None
                session["next_action"] = "idle"
            self._set_latest_card(session, build_turn_card(session, "Peer Agent", message_text))

        issuer = str(payload.get("issuer_did") or "").strip()
        if issuer:
            session["remote_did"] = issuer

        session["updated_at"] = now
        session["status"] = "completed" if self._stop_due_to_limit(session) else "active"
        self._save_session(session)
        return session

    def record_local_turn(self, session_id: str, text: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if not session:
            return None
        if text.strip():
            self._append_event(session, {"event": "local.compose", "speaker": "local", "text": text.strip()})
            self._append_transcript(session, "local", text.strip())
            session["last_speaker"] = "local"
            session["waiting_on"] = "remote"
            session["next_action"] = "await_remote"
            self._set_latest_card(session, build_turn_card(session, "You", text.strip()))
            session["updated_at"] = self._ts()
            self._save_session(session)
        return session

    def mark_error(self, session_id: str, error: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if not session:
            return None
        session["last_error"] = error
        session["status"] = "error"
        session["waiting_on"] = None
        session["next_action"] = "idle"
        self._append_event(session, {"event": "session.error", "speaker": "system", "text": error})
        self._set_latest_status_card(session)
        session["updated_at"] = self._ts()
        self._save_session(session)
        return session

    def mark_paused(self, session_id: str, reason: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if not session:
            return None
        session["status"] = "paused"
        session["waiting_on"] = None
        session["next_action"] = "idle"
        session["pause_reason"] = reason
        session["updated_at"] = self._ts()
        self._append_event(session, {"event": "session.paused", "speaker": "system", "text": reason})
        self._set_latest_status_card(session)
        self._save_session(session)
        return session

    def complete_session(self, session_id: str, reason: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if not session:
            return None
        session["status"] = "completed"
        session["waiting_on"] = None
        session["next_action"] = "idle"
        session["completed_reason"] = reason
        session["updated_at"] = self._ts()
        self._append_event(session, {"event": "session.completed", "speaker": "system", "text": reason})
        self._set_latest_status_card(session)
        self._save_session(session)
        return session

    def list_runnable_sessions(self) -> list[dict[str, Any]]:
        now = time.time()
        runnable: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            session = self._load_session(path)
            if not session:
                continue
            if session.get("status") != "active":
                continue
            if not session.get("auto_continue"):
                continue
            if session.get("next_action") != "compose_local_turn":
                continue
            if float(session.get("next_action_at") or 0) > now:
                continue
            runnable.append(session)
        return runnable

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        return self._load_session(self._session_path(session_id))

    def find_by_context(self, context_id: str) -> dict[str, Any] | None:
        if not context_id:
            return None
        for path in sorted(self.root.glob("*.json")):
            session = self._load_session(path)
            if session and session.get("context_id") == context_id:
                return session
        return None

    def find_active_session(
        self,
        *,
        remote_agent: str | None = None,
        context_id: str | None = None,
        origin_platform: str | None = None,
        origin_chat_id: str | None = None,
    ) -> dict[str, Any] | None:
        remote_agent = str(remote_agent or "").strip().rstrip("/")
        context_id = str(context_id or "").strip()
        origin_platform = str(origin_platform or "").strip().lower()
        origin_chat_id = str(origin_chat_id or "").strip()

        if context_id:
            session = self.find_by_context(context_id)
            if session and session.get("status") == "active":
                if not remote_agent or str(session.get("remote_agent") or "").strip().rstrip("/") == remote_agent:
                    return session

        if not origin_platform or not origin_chat_id:
            return None

        for path in sorted(self.root.glob("*.json")):
            session = self._load_session(path)
            if not session or session.get("status") != "active":
                continue
            origin = session.get("origin") or {}
            if str(origin.get("platform") or "").strip().lower() != origin_platform:
                continue
            if str(origin.get("chat_id") or "").strip() != origin_chat_id:
                continue
            if remote_agent and str(session.get("remote_agent") or "").strip().rstrip("/") != remote_agent:
                continue
            return session
        return None

    def note_worker_claim(self, session_id: str, delay_seconds: float = 30.0) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if not session:
            return None
        session["next_action_at"] = time.time() + max(delay_seconds, 1.0)
        session["updated_at"] = self._ts()
        self._save_session(session)
        return session

    def serialize_for_response(self, session: dict[str, Any] | None) -> dict[str, Any] | None:
        if not session:
            return None
        keys = (
            "session_id",
            "context_id",
            "status",
            "goal",
            "topic",
            "auto_continue",
            "max_turns",
            "turn_count",
            "last_speaker",
            "waiting_on",
            "next_action",
            "remote_agent",
            "remote_did",
            "origin",
            "updated_at",
            "created_at",
            "last_error",
        )
        data = {key: session.get(key) for key in keys}
        data["recent_messages"] = list(session.get("recent_messages") or [])
        data["latest_card"] = dict(session.get("latest_card") or {})
        data["recent_cards"] = list(session.get("recent_cards") or [])
        return data

    def get_remote_api_key(self, session_id: str) -> str:
        if not session_id:
            return ""
        return str(self._remote_api_keys.get(self._normalize_session_id(session_id)) or "").strip()

    def _new_session(self, session_id: str, now: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "status": "active",
            "goal": "",
            "topic": "",
            "auto_continue": False,
            "max_turns": None,
            "turn_count": 0,
            "last_speaker": None,
            "waiting_on": None,
            "next_action": "idle",
            "next_action_at": 0,
            "context_id": "",
            "remote_agent": "",
            "remote_did": "",
            "origin": {},
            "recent_messages": [],
            "recent_events": [],
            "latest_card": {},
            "recent_cards": [],
            "created_at": now,
            "updated_at": now,
            "controller_mode": "none",
        }

    def _stop_due_to_limit(self, session: dict[str, Any]) -> bool:
        max_turns = session.get("max_turns")
        if max_turns in (None, ""):
            return False
        try:
            return int(session.get("turn_count") or 0) >= int(max_turns)
        except Exception:
            return False

    def _append_transcript(self, session: dict[str, Any], speaker: str, text: str) -> None:
        messages = list(session.get("recent_messages") or [])
        messages.append({"speaker": speaker, "text": text.strip(), "at": self._ts()})
        session["recent_messages"] = messages[-12:]

    def _append_event(self, session: dict[str, Any], event: dict[str, Any]) -> None:
        events = list(session.get("recent_events") or [])
        payload = {key: value for key, value in event.items() if value not in (None, "")}
        payload["at"] = self._ts()
        events.append(payload)
        session["recent_events"] = events[-30:]
        self._append_jsonl(self._events_path(str(session["session_id"])), payload)

    def _save_session(self, session: dict[str, Any]) -> None:
        session_id = self._normalize_session_id(str(session["session_id"]))
        session["session_id"] = session_id
        session.pop("remote_api_key", None)
        path = self._session_path(session_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def _set_latest_card(self, session: dict[str, Any], card: dict[str, str]) -> None:
        cards = list(session.get("recent_cards") or [])
        cards.append({"at": self._ts(), **card})
        session["recent_cards"] = cards[-12:]
        session["latest_card"] = dict(card)

    def _set_latest_status_card(self, session: dict[str, Any]) -> None:
        self._set_latest_card(session, build_status_card(session))

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
            handle.write("\n")

    def _extract_message_text(self, message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        parts = message.get("parts") or []
        text_parts: list[str] = []
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"].strip())
        return "\n".join(chunk for chunk in text_parts if chunk).strip()

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_session(self, path: Path) -> dict[str, Any] | None:
        session = self._read_json(path)
        if not isinstance(session, dict):
            return None
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return None
        try:
            normalized_session_id = self._normalize_session_id(session_id)
        except ValueError:
            return None
        if normalized_session_id != session_id:
            session["session_id"] = normalized_session_id
        if session.pop("remote_api_key", None):
            self._save_session(session)
        return session

    def _session_path(self, session_id: str) -> Path:
        return self._resolve_session_path(self._normalize_session_id(session_id), ".json")

    def _events_path(self, session_id: str) -> Path:
        return self._resolve_session_path(self._normalize_session_id(session_id), ".events.jsonl")

    def _resolve_session_path(self, session_id: str, suffix: str) -> Path:
        path = (self.root / f"{session_id}{suffix}").resolve()
        path.relative_to(self.root.resolve())
        return path

    def _normalize_session_id(self, session_id: str) -> str:
        value = str(session_id or "").strip()
        if not value:
            raise ValueError("session_id is required")
        if "/" in value or "\\" in value or ".." in value:
            raise ValueError("Invalid session_id")
        return value

    def _ts(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
