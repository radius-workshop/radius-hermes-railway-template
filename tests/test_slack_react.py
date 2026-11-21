"""Offline tests for the Slack reaction plugin: targeting, scoping, Slack errors, loop dispatch."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/slack-react"
spec = importlib.util.spec_from_file_location("template_slack_react", PLUGIN / "__init__.py",
                                              submodule_search_locations=[str(PLUGIN)])
sr = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sr
spec.loader.exec_module(sr)

CHANNEL, THREAD, MESSAGE = "C0123456789", "1760649980.000100", "1760649990.000200"
SESSION = {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_CHAT_ID": CHANNEL,
           "HERMES_SESSION_THREAD_ID": THREAD, "HERMES_SESSION_MESSAGE_ID": MESSAGE,
           "HERMES_SESSION_SCOPE_ID": "T0123456"}


class SlackRefusal(Exception):
    """Shaped like slack_sdk.errors.SlackApiError: the error code is in ``response``."""

    def __init__(self, code):
        super().__init__(f"The request to the Slack API failed. (error: {code}) xoxb-not-a-real-token")
        self.response = {"ok": False, "error": code}


class FakeClient:
    def __init__(self, fail=None, delay=0.0):
        self.calls, self.threads, self.fail, self.delay = [], [], fail, delay

    async def _record(self, method, **kwargs):
        self.calls.append((method, kwargs))
        self.threads.append(threading.current_thread())
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return {"ok": True}

    async def reactions_add(self, **kwargs):
        return await self._record("add", **kwargs)

    async def reactions_remove(self, **kwargs):
        return await self._record("remove", **kwargs)


class FakeApp:
    def __init__(self, client):
        self.client = client


class Base(unittest.TestCase):
    session = SESSION

    def setUp(self):
        self.client = FakeClient()
        saved = dict(sr._live)
        self.addCleanup(sr._live.update, saved)
        sr._live.update(app=FakeApp(self.client), adapter=None, loop=None)
        cleared = {k: "" for k in SESSION}
        env = patch.dict(os.environ, {**cleared, **self.session})
        env.start()
        self.addCleanup(env.stop)

    def react(self, **params):
        return json.loads(asyncio.run(sr._handler(params)))


class TargetTests(Base):
    def test_defaults_to_the_message_that_started_the_turn(self):
        out = self.react(emoji=":Radius:")
        self.assertEqual(out, {"ok": True, "action": "added", "emoji": "radius", "message_ts": MESSAGE,
                               "target": "message"})
        self.assertEqual(self.client.calls, [("add", {"channel": CHANNEL, "timestamp": MESSAGE, "name": "radius"})])

    def test_thread_parent_and_explicit_timestamp(self):
        self.assertEqual(self.react(emoji="tada", target="thread_parent")["message_ts"], THREAD)
        out = self.react(emoji="tada", target="thread_parent", message_ts="1760640000.000001")
        self.assertEqual((out["message_ts"], out["target"]), ("1760640000.000001", "message_ts"))
        self.assertEqual([c[1]["timestamp"] for c in self.client.calls], [THREAD, "1760640000.000001"])

    def test_remove(self):
        self.assertEqual(self.react(emoji="tada", remove=True)["action"], "removed")
        self.assertEqual(self.client.calls[0][0], "remove")

    def test_the_channel_comes_from_the_session_not_the_model(self):
        self.react(emoji="tada", channel="C9999999999", chat_id="C9999999999", target="message")
        self.assertEqual(self.client.calls[0][1]["channel"], CHANNEL)

    def test_bad_input_is_refused_before_calling_slack(self):
        for params, expect in (
            ({}, "emoji is required"),
            ({"emoji": "🎉"}, "emoji name"),
            ({"emoji": "has space"}, "emoji name"),
            ({"emoji": "tada", "target": "everything"}, "target must be one of"),
            ({"emoji": "tada", "message_ts": "p1760649990000200"}, "message_ts must be"),
            ({"emoji": "tada", "message_ts": "1760649990.000200&channel=C1"}, "message_ts must be"),
        ):
            out = self.react(**params)
            self.assertFalse(out["ok"], params)
            self.assertIn(expect, out["error"], params)
        self.assertEqual(self.client.calls, [])

    def test_emoji_names(self):
        for raw, name in ((":+1:", "+1"), ("white_check_mark", "white_check_mark"), (" :man-shrugging: ", "man-shrugging"),
                          ("thumbsup::skin-tone-3", "thumbsup::skin-tone-3")):
            self.assertEqual(sr.normalize_emoji(raw), name)


class SessionTests(Base):
    def test_outside_a_thread_there_is_no_thread_parent(self):
        with patch.dict(os.environ, {"HERMES_SESSION_THREAD_ID": ""}):
            out = self.react(emoji="tada", target="thread_parent")
        self.assertIn("not in a thread", out["error"])

    def test_unknown_triggering_message_asks_for_a_timestamp(self):
        with patch.dict(os.environ, {"HERMES_SESSION_MESSAGE_ID": ""}):
            self.assertIn("pass message_ts", self.react(emoji="tada")["error"])
            self.assertTrue(self.react(emoji="tada", message_ts=MESSAGE)["ok"])

    def test_only_slack_conversations(self):
        for override in ({"HERMES_SESSION_PLATFORM": "telegram"}, {"HERMES_SESSION_PLATFORM": ""},
                         {"HERMES_SESSION_CHAT_ID": ""}):
            with patch.dict(os.environ, override):
                self.assertFalse(self.react(emoji="tada")["ok"], override)
        self.assertEqual(self.client.calls, [])

    def test_not_connected(self):
        sr._live.update(app=None)
        self.assertIn("not connected", self.react(emoji="tada")["error"])

    def test_workspace_client_is_chosen_by_the_session_team(self):
        other = FakeClient()
        asked = []

        class Adapter:
            def _get_client(self, chat_id, team_id=None):
                asked.append((chat_id, team_id))
                return other

        sr._live.update(adapter=Adapter())
        self.assertTrue(self.react(emoji="tada")["ok"])
        self.assertEqual(asked, [(CHANNEL, "T0123456")])
        self.assertEqual((len(other.calls), len(self.client.calls)), (1, 0))

    def test_a_broken_workspace_lookup_falls_back_to_the_app_client(self):
        class Adapter:
            def _get_client(self, chat_id, team_id=None):
                raise KeyError(team_id)

        sr._live.update(adapter=Adapter())
        self.assertTrue(self.react(emoji="tada")["ok"])
        self.assertEqual(len(self.client.calls), 1)


class SlackErrorTests(Base):
    def test_repeating_a_reaction_is_not_an_error(self):
        self.client.fail = SlackRefusal("already_reacted")
        self.assertEqual((self.react(emoji="tada")["ok"], self.react(emoji="tada")["action"]), (True, "unchanged"))
        self.client.fail = SlackRefusal("no_reaction")
        self.assertEqual(self.react(emoji="tada", remove=True)["action"], "unchanged")

    def test_slack_refusals_are_explained(self):
        for code, expect in (("invalid_name", "no emoji with that name"), ("message_not_found", "no message"),
                             ("missing_scope", "reactions:write"), ("some_new_code", "refused")):
            self.client.fail = SlackRefusal(code)
            out = self.react(emoji="radius")
            self.assertEqual((out["ok"], out["error"]), (False, code))
            self.assertIn(expect, out["detail"])

    def test_already_reacted_on_remove_is_still_an_error(self):
        self.client.fail = SlackRefusal("already_reacted")
        self.assertFalse(self.react(emoji="tada", remove=True)["ok"])

    def test_other_failures_do_not_leak_details(self):
        self.client.fail = RuntimeError("Bearer xoxb-secret-token refused")
        with self.assertLogs(sr.logger, "WARNING") as logs:
            out = self.react(emoji="tada")
        self.assertEqual(out, {"ok": False, "error": "the Slack API call failed"})
        self.assertNotIn("xoxb", "".join(logs.output))

    def test_timeout(self):
        self.client.delay = 5
        with patch.object(sr, "TIMEOUT_SECONDS", 0.05):
            self.assertIn("did not answer in time", self.react(emoji="tada")["error"])

    def test_real_slack_sdk_error_shape(self):
        try:
            from slack_sdk.errors import SlackApiError
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError as err:  # pragma: no cover - depends on the environment
            self.skipTest(f"slack_sdk is not installed: {err}")
        self.assertTrue(callable(AsyncWebClient.reactions_add) and callable(AsyncWebClient.reactions_remove))
        self.client.fail = SlackApiError("failed", {"ok": False, "error": "invalid_name"})
        self.assertEqual(self.react(emoji="nope")["error"], "invalid_name")


class GatewayLoopTests(Base):
    def test_the_call_runs_on_the_loop_the_app_was_connected_on(self):
        gateway_loop = asyncio.new_event_loop()
        gateway = threading.Thread(target=gateway_loop.run_forever, daemon=True)
        gateway.start()
        self.addCleanup(gateway.join, 5)
        self.addCleanup(gateway_loop.call_soon_threadsafe, gateway_loop.stop)

        async def connect():
            return sr.wire_slack(FakeApp(self.client), None)

        self.assertTrue(asyncio.run_coroutine_threadsafe(connect(), gateway_loop).result(5))
        self.assertIs(sr._live["loop"], gateway_loop)
        self.assertTrue(self.react(emoji="tada")["ok"])  # asyncio.run: a different loop, as in a tool worker
        self.assertEqual(self.client.threads, [gateway])

    def test_wiring_without_a_loop_or_reactions_api(self):
        self.assertTrue(sr.wire_slack(FakeApp(self.client)))
        self.assertIsNone(sr._live["loop"])
        with self.assertLogs(sr.logger, "WARNING"):
            self.assertFalse(sr.wire_slack(object()))
        self.assertIs(sr._live["app"].client, self.client)


class RegistrationTests(Base):
    def test_registers_an_async_tool_and_the_slack_factory(self):
        tools, factories = [], []

        class Ctx:
            def register_tool(self, **kwargs):
                tools.append(kwargs)

            def register_platform_handler(self, platform, factory):
                factories.append((platform, factory))

        sr.register(Ctx())
        self.assertEqual(factories, [("slack", sr.wire_slack)])
        tool = tools[0]
        self.assertEqual((tool["name"], tool["toolset"], tool["is_async"]), ("slack_react", "slack-react", True))
        self.assertEqual(tool["schema"]["parameters"]["required"], ["emoji"])
        self.assertNotIn("channel", tool["schema"]["parameters"]["properties"])
        result = asyncio.run(tool["handler"]({"emoji": "tada"}, task_id="t"))
        self.assertTrue(json.loads(result)["ok"])

    def test_an_old_hermes_without_the_plugin_api_still_loads(self):
        class Ctx:
            def register_tool(self, **kwargs):
                pass

        with self.assertLogs(sr.logger, "WARNING"):
            sr.register(Ctx())

    def test_manifest_matches_the_registered_tool(self):
        manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["provides_tools"], manifest["toolsets"]),
                         ("slack-react", ["slack_react"], ["slack-react"]))


class PinnedHermesContractTests(unittest.TestCase):
    """Runs only where the pinned Hermes is importable (the image, or a dev venv with it installed)."""

    def test_session_variables_and_plugin_api(self):
        try:
            from gateway import session_context
            import hermes_cli.plugins as hermes_plugins
            from plugins.platforms.slack.adapter import SlackAdapter
        except Exception as err:  # pragma: no cover - depends on the environment
            self.skipTest(f"pinned Hermes is not importable: {err}")
        names = {var.name for var in session_context._SESSION_VARS}
        self.assertLessEqual(set(SESSION), names)
        self.assertTrue(callable(getattr(hermes_plugins.PluginContext, "register_platform_handler", None)))
        self.assertTrue(callable(getattr(SlackAdapter, "_get_client", None)))


class DocsTests(unittest.TestCase):
    def test_docs_and_prompts_name_the_tool(self):
        for name in ("README.md", "HERMES.md", "skills/slack-react.md"):
            self.assertIn("slack_react", (ROOT / name).read_text(encoding="utf-8"), name)
        manifest = json.loads((ROOT / "slack/manifest.json").read_text(encoding="utf-8"))
        self.assertIn("reactions:write", manifest["oauth_config"]["scopes"]["bot"])


if __name__ == "__main__":
    unittest.main()
