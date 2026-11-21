"""Let the agent add or remove an emoji reaction on a Slack message.

Hermes at v2026.9.24 reacts to Slack messages only by itself (the 👀 → ✅/❌ processing
acknowledgement). Its ``send_message`` ``react`` action needs a public ``add_reaction`` on the
adapter, which the Slack adapter does not have, and ``ctx.platform_actions.add_reaction`` covers
Telegram and Discord only. So the model has no way to react on request.

This plugin keeps a reference to the live slack_bolt app, handed over by the public
``ctx.register_platform_handler("slack", ...)`` API, and calls Slack's ``reactions.add`` /
``reactions.remove`` with it. A reaction can only go on a message in the conversation the current
turn came from: the channel is read from the session, never from the model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
# Slack emoji names: lowercase letters, digits, _ + - ', optionally a skin-tone modifier.
_EMOJI_NAME = re.compile(r"[a-z0-9_+'\-]{1,100}(::skin-tone-[2-6])?")
_SLACK_TS = re.compile(r"\d{6,}\.\d{1,9}")
TARGETS = ("message", "thread_parent")

# What Slack's error codes mean for the person who asked. Anything else is passed through as is.
_ERRORS = {
    "invalid_name": "no emoji with that name exists in this workspace",
    "message_not_found": "no message with that timestamp in this conversation",
    "bad_timestamp": "no message with that timestamp in this conversation",
    "channel_not_found": "the bot cannot see this conversation",
    "not_in_channel": "the bot is not a member of this channel",
    "is_archived": "this channel is archived",
    "too_many_emoji": "this message already has the maximum number of different emoji",
    "too_many_reactions": "the bot has reached Slack's limit of reactions on this message",
    "missing_scope": "the Slack app lacks the reactions:write scope; reinstall it from slack/manifest.json",
    "not_reactable": "Slack does not allow reactions on this message",
    "thread_locked": "this thread is locked",
    "ratelimited": "Slack rate limit reached; try again shortly",
}

# The live Slack app, set when the adapter connects. Process-local, like the gateway itself.
_live: dict = {"app": None, "adapter": None, "loop": None}


class ReactError(Exception):
    """A problem worth showing to the model as is."""


def wire_slack(app, adapter=None) -> bool:
    """``register_platform_handler("slack", ...)`` factory: remember the app the gateway connected."""
    if getattr(getattr(app, "client", None), "reactions_add", None) is None:
        logger.warning("[slack-react] Slack app has no reactions API; slack_react will not work")
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    _live.update(app=app, adapter=adapter, loop=loop)
    logger.info("[slack-react] ready to add reactions")
    return True


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        def get_session_env(key, default=""):
            return os.environ.get(key, default)
    return str(get_session_env(name, "") or "").strip()


def normalize_emoji(value) -> str:
    """``:Radius:`` → ``radius``. Raises for anything that is not an emoji name."""
    name = str(value or "").strip().strip(":").strip().lower()
    if not name:
        raise ReactError("emoji is required: pass its name, for example white_check_mark or radius")
    if not _EMOJI_NAME.fullmatch(name):
        raise ReactError("emoji must be a Slack emoji name such as thumbsup, white_check_mark or a "
                         "workspace custom emoji name; a pasted emoji character is not accepted")
    return name


def resolve_message(params: dict) -> tuple[str, str]:
    """``(ts, which)`` for the message to react to, from the session unless ``message_ts`` is given."""
    explicit = str(params.get("message_ts") or "").strip()
    if explicit:
        if not _SLACK_TS.fullmatch(explicit):
            raise ReactError("message_ts must be a Slack message timestamp such as 1760649990.000200")
        return explicit, "message_ts"
    target = str(params.get("target") or "message").strip().lower()
    if target not in TARGETS:
        raise ReactError(f"target must be one of: {', '.join(TARGETS)}")
    if target == "thread_parent":
        ts = _session("HERMES_SESSION_THREAD_ID")
        if not _SLACK_TS.fullmatch(ts):
            raise ReactError("this conversation is not in a thread, so there is no thread parent")
        return ts, target
    ts = _session("HERMES_SESSION_MESSAGE_ID")
    if not _SLACK_TS.fullmatch(ts):
        raise ReactError("the message that started this turn is unknown; pass message_ts")
    return ts, target


def _client(channel: str):
    """The WebClient for this conversation's workspace; the app's own client as a fallback."""
    app, adapter = _live["app"], _live["adapter"]
    if app is None:
        raise ReactError("Slack is not connected in this process, so reactions are unavailable")
    get_client = getattr(adapter, "_get_client", None)  # multi-workspace installs
    if callable(get_client):
        try:
            client = get_client(channel, team_id=_session("HERMES_SESSION_SCOPE_ID") or None)
            if getattr(client, "reactions_add", None) is not None:
                return client
        except Exception as err:
            logger.debug("[slack-react] workspace client lookup failed: %s", type(err).__name__)
    return app.client


async def _call(method, **kwargs):
    """Await a Slack API call on the gateway's loop when the tool runs on another one."""
    loop = _live["loop"]
    if loop is not None and loop.is_running() and loop is not asyncio.get_running_loop():
        future = asyncio.run_coroutine_threadsafe(method(**kwargs), loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), TIMEOUT_SECONDS)
        except BaseException:
            future.cancel()
            raise
    return await asyncio.wait_for(method(**kwargs), TIMEOUT_SECONDS)


def _slack_error(err: Exception) -> str:
    response = getattr(err, "response", None)
    try:
        code = response.get("error") if response is not None else None
    except Exception:
        code = None
    return str(code) if code else ""


async def slack_react(params: dict) -> dict:
    if _session("HERMES_SESSION_PLATFORM").lower() != "slack":
        raise ReactError("slack_react only works in a Slack conversation")
    channel = _session("HERMES_SESSION_CHAT_ID")
    if not channel:
        raise ReactError("this session has no Slack conversation")
    emoji = normalize_emoji(params.get("emoji"))
    ts, which = resolve_message(params)
    remove = bool(params.get("remove"))
    client = _client(channel)
    out = {"ok": True, "action": "removed" if remove else "added", "emoji": emoji, "message_ts": ts, "target": which}
    try:
        await _call(client.reactions_remove if remove else client.reactions_add,
                    channel=channel, timestamp=ts, name=emoji)
    except asyncio.TimeoutError:
        raise ReactError("Slack did not answer in time; the reaction may or may not have been applied") from None
    except Exception as err:
        code = _slack_error(err)
        if code == "already_reacted" and not remove:
            return {**out, "action": "unchanged", "note": "the bot had already added this reaction"}
        if code == "no_reaction" and remove:
            return {**out, "action": "unchanged", "note": "the bot had not added this reaction"}
        if not code:
            logger.warning("[slack-react] reaction call failed: %s", type(err).__name__)
            raise ReactError("the Slack API call failed") from None
        logger.info("[slack-react] Slack refused %s on %s: %s", emoji, ts, code)
        return {"ok": False, "error": code, "detail": _ERRORS.get(code, "Slack refused the reaction"),
                "emoji": emoji, "message_ts": ts}
    return out


async def _handler(params, **_kwargs):
    try:
        return json.dumps(await slack_react(params or {}), ensure_ascii=False)
    except ReactError as err:
        return json.dumps({"ok": False, "error": str(err)})
    except Exception as err:
        logger.exception("[slack-react] tool failed")
        return json.dumps({"ok": False, "error": f"tool failed ({type(err).__name__})"})


def register(ctx):
    ctx.register_tool(
        name="slack_react", toolset="slack-react", is_async=True,
        schema={"name": "slack_react",
                "description": ("Add (or remove) an emoji reaction on a message in the current Slack conversation. "
                                "Works with standard emoji and this workspace's custom emoji, by name. Defaults "
                                "to the message that started this turn."),
                "parameters": {"type": "object", "properties": {
                    "emoji": {"type": "string",
                              "description": "emoji name with or without colons, e.g. white_check_mark, tada, radius"},
                    "target": {"type": "string", "enum": list(TARGETS),
                               "description": ("message (default): the message that started this turn. "
                                               "thread_parent: the first message of this thread")},
                    "message_ts": {"type": "string",
                                   "description": "Slack ts of another message in this conversation; overrides target"},
                    "remove": {"type": "boolean", "description": "remove the bot's reaction instead of adding it"},
                }, "required": ["emoji"]}},
        handler=_handler)
    register_platform_handler = getattr(ctx, "register_platform_handler", None)
    if callable(register_platform_handler):
        register_platform_handler("slack", wire_slack)
    else:
        logger.warning("[slack-react] this Hermes has no register_platform_handler; slack_react will not work")
