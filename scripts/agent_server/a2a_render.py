"""User-facing rendering helpers for managed A2A session updates."""
from __future__ import annotations

from typing import Any


def build_turn_card(session: dict[str, Any], speaker_label: str, text: str) -> dict[str, str]:
    turn_count = int(session.get("turn_count") or 0)
    max_turns = session.get("max_turns")
    turn_label = f"{turn_count}/{max_turns}" if max_turns not in (None, "") else f"{turn_count}/open"
    title = f"Turn {turn_label} • {speaker_label}"
    body = _normalize_text(text)
    footer = _status_footer(session)
    return {"title": title, "body": body, "footer": footer, "text": _as_platform_text(title, body, footer)}


def build_status_card(session: dict[str, Any]) -> dict[str, str]:
    title = "Conversation Active" if session.get("status") == "active" else "Conversation Update"
    body_lines = []
    topic = str(session.get("topic") or session.get("goal") or "").strip()
    if topic:
        body_lines.append(topic)
    waiting_on = session.get("waiting_on")
    if waiting_on == "remote":
        body_lines.append("Waiting on the peer agent.")
    elif waiting_on == "local":
        body_lines.append("Preparing the next local turn.")
    else:
        body_lines.append("No pending turn.")
    footer = _status_footer(session)
    body = "\n".join(body_lines)
    return {"title": title, "body": body, "footer": footer, "text": _as_platform_text(title, body, footer)}


def _status_footer(session: dict[str, Any]) -> str:
    waiting_on = session.get("waiting_on")
    if waiting_on == "remote":
        wait_text = "Waiting on peer"
    elif waiting_on == "local":
        wait_text = "Preparing next turn"
    else:
        wait_text = "Idle"
    context_id = str(session.get("context_id") or "").strip()
    tail = context_id[-8:] if context_id else "unknown"
    return f"{wait_text} • Context {tail}"


def _as_platform_text(title: str, body: str, footer: str) -> str:
    chunks = [title.strip()]
    if body.strip():
        chunks.append("")
        chunks.append(body.strip())
    if footer.strip():
        chunks.append("")
        chunks.append(footer.strip())
    return "\n".join(chunks).strip()


def _normalize_text(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines()]
    filtered = [line for line in lines if line]
    collapsed = "\n".join(filtered)
    return collapsed[:1200].strip()
