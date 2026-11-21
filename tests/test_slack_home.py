"""Offline tests for the Slack App Home tab plugin: data, views, authorization, Slack wiring."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, directory):
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugins" / directory / "__init__.py",
                                                  submodule_search_locations=[str(ROOT / "plugins" / directory)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sh = load("template_slack_home", "slack-home")

ADDRESS = "0x3f2a000000000000000000000000000000009a2c"
USER, TEAM, APP = "U012ABCDEF", "T0123456", "A0123ABCD"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TX = "0x" + "ab" * 32
BALANCES = {("base-mainnet", "token"): Decimal("812.40"), ("radius-mainnet", "token"): Decimal("402.10"),
            ("base-mainnet", "native"): Decimal("0.0214"),
            ("radius-testnet", "token"): Decimal("412.5"), ("base-sepolia", "token"): Decimal("18.2")}


def row(id_, outcome="settlement_reported", network="eip155:723487", amount="0.05", ts="2026-11-20T09:12:00+00:00",
        url="https://grug402.dev/weather", requested_by=USER, **extra):
    return {"id": id_, "ts": ts, "url": url, "method": "GET", "outcome": outcome, "network": network, "amount": amount,
            "asset_symbol": "SBC", "requested_by": requested_by, "event": "completed", **extra}


LEDGER = [
    row("a", explorer_url=f"https://network.radiustech.xyz/tx/{TX}"),
    row("b", network="eip155:8453", amount="0.01", ts="2026-11-20T08:47:00+00:00", url="https://grug402.dev/joke"),
    row("c", outcome="refused", amount="2", ts="2026-11-20T08:30:00+00:00"),
    row("d", outcome="quoted", network="eip155:8453", amount="0.10", ts="2026-11-19T10:00:00+00:00"),
    row("e", network="eip155:72344", amount="5", ts="2026-11-20T07:00:00+00:00"),            # testnet
    row("f", outcome="error", network=None, amount=None),                                    # never priced
    row("g", outcome="no_payment_required", ts="2026-11-20T06:00:00+00:00"),
]


def snapshot(**overrides):
    return {"taken_at": NOW.timestamp(), "address": ADDRESS, "balances": dict(BALANCES), "ledger": list(LEDGER),
            "feedback": {"good": 18, "bad": 3}, **overrides}


def text_of(view) -> str:
    return json.dumps(view, ensure_ascii=False)


class FakeClient:
    def __init__(self, fail=None):
        self.published, self.opened, self.fail, self.fail_tables_only = [], [], fail, False

    async def views_publish(self, **kwargs):
        if self.fail and (not self.fail_tables_only or '"table"' in json.dumps(kwargs["view"])):
            raise self.fail
        self.published.append(kwargs)

    async def views_open(self, **kwargs):
        self.opened.append(kwargs)


class FakeApp:
    def __init__(self):
        self.client, self.middleware, self.views = FakeClient(), [], {}

    def use(self, fn):
        self.middleware.append(fn)

    def view(self, callback_id):
        return lambda fn: self.views.setdefault(callback_id, fn)


class FakeAdapter:
    def __init__(self, allowed=(USER,)):
        self.allowed, self.asked = set(allowed), []

    def _is_interactive_user_authorized(self, user_id, *, channel_id="", user_name=None, team_id=""):
        self.asked.append((user_id, channel_id, team_id))
        return user_id in self.allowed


ENV = {"X402_PAID_NETWORKS": "radius-mainnet,base-mainnet", "X402_ALLOWED_HOSTS": "*", "X402_MAX_PER_CALL": "",
       "SLACK_HOME": "", "SLACK_HOME_ETH_USD": "", "SLACK_HOME_ETH_FLOOR": "", "SLACK_HOME_CACHE_SECONDS": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.ledger = Path(self.home.name) / "ledger.jsonl"
        env = patch.dict(os.environ, {**ENV, "HERMES_HOME": self.home.name, "X402_LEDGER_PATH": str(self.ledger)})
        env.start()
        self.addCleanup(env.stop)
        saved_live, saved_cache = dict(sh._live), dict(sh._cache)
        self.addCleanup(sh._live.update, saved_live)
        self.addCleanup(sh._cache.update, saved_cache)
        self.addCleanup(sh._dm_channels.clear)
        self.app, self.adapter = FakeApp(), FakeAdapter()
        sh._live.update(app=self.app, adapter=self.adapter, loop=None, dispatch=self.dispatch)
        sh._cache.update(snapshot=None, address=None)
        self.tool_calls = []

    def dispatch(self, name, args):
        self.tool_calls.append((name, args))
        if name == "radius_wallet_address":
            return json.dumps({"address": ADDRESS})
        if name == "feedback_ledger":
            return json.dumps({"summary": {"good": 18, "bad": 3}, "entries": []})
        return json.dumps({"error": "unknown"})

    def write_ledger(self, rows):
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class DataTests(Base):
    def test_networks_match_the_x402_plugin(self):
        x402 = load("template_x402_for_slack_home", "x402")
        self.assertEqual(set(sh.NETWORKS), set(x402.NETWORKS))
        for name, spec in x402.NETWORKS.items():
            mine = {k: v for k, v in sh.NETWORKS[name].items() if k != "label"}
            self.assertEqual(mine, spec, name)

    def test_ledger_keeps_the_latest_row_per_attempt(self):
        self.write_ledger([{**row("a"), "event": "started", "outcome": "pending"}, row("a"), row("b")])
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write("not json\n[1]\n")
        rows = sh.read_ledger()
        self.assertEqual([(r["id"], r["outcome"]) for r in rows], [("a", "settlement_reported"), ("b", "settlement_reported")])
        self.assertEqual(sh.read_ledger(Path(self.home.name) / "missing.jsonl"), [])

    def test_payments_are_filtered_by_environment_and_sorted(self):
        self.assertEqual([r["id"] for r in sh.payments(LEDGER, "mainnet")], ["a", "b", "c", "d"])
        self.assertEqual([r["id"] for r in sh.payments(LEDGER, "testnet")], ["e"])
        bad = [row("x", ts="yesterday"), row("y", amount="lots"), row("z", network="eip155:1"), {"id": "w"}]
        self.assertEqual(sh.payments(bad, "mainnet"), [])

    def test_spent_today_counts_only_settled_payments_from_today(self):
        self.assertEqual(sh.spent_today(sh.payments(LEDGER, "mainnet"), NOW), Decimal("0.06"))

    def test_payment_environment_follows_the_enabled_networks(self):
        self.assertEqual(sh.payment_environment(), "mainnet")
        with patch.dict(os.environ, {"X402_PAID_NETWORKS": "radius-testnet,base-testnet,eip155:84532,nonsense"}):
            self.assertEqual(sh.paid_networks(), ["radius-testnet", "base-sepolia"])
            self.assertEqual(sh.payment_environment(), "testnet")

    def test_balances_are_read_with_read_only_calls(self):
        calls = []

        def rpc(url, method, params):
            calls.append((url, method, params))
            if method == "eth_getBalance":
                return 21_400_000_000_000_000
            if "sepolia" in url:
                raise TimeoutError()
            return 812_400_000

        with patch.object(sh, "_rpc", rpc), self.assertLogs(sh.logger, "WARNING"):
            balances = sh.read_balances(ADDRESS)
        self.assertEqual(balances[("base-mainnet", "token")], Decimal("812.4"))
        self.assertEqual(balances[("base-mainnet", "native")], Decimal("0.0214"))
        self.assertIsNone(balances[("base-sepolia", "token")])
        self.assertEqual({method for _url, method, _params in calls}, {"eth_call", "eth_getBalance"})
        call = next(params for url, method, params in calls if method == "eth_call" and url == "https://mainnet.base.org")
        self.assertEqual(call[0], {"to": sh.NETWORKS["base-mainnet"]["asset"],
                                   "data": "0x70a08231" + ADDRESS[2:].rjust(64, "0")})

    def test_snapshot_is_cached_and_refresh_is_rate_limited(self):
        reads = []
        with patch.object(sh, "read_balances", lambda address: reads.append(address) or dict(BALANCES)):
            first = sh.take_snapshot()
            self.assertIs(sh.take_snapshot(), first)
            self.assertIs(sh.take_snapshot(force=True), first)  # under MIN_REFRESH_SECONDS old
            first["taken_at"] -= sh.MIN_REFRESH_SECONDS + 1
            self.assertIsNot(sh.take_snapshot(force=True), first)
        self.assertEqual(reads, [ADDRESS, ADDRESS])
        self.assertEqual(first["feedback"], {"good": 18, "bad": 3})
        self.assertEqual([c for c in self.tool_calls if c[0] == "radius_wallet_address"], [("radius_wallet_address", {})])

    def test_a_failing_wallet_tool_gives_no_balances(self):
        sh._live["dispatch"] = lambda name, args: (_ for _ in ()).throw(RuntimeError("radius-cli missing"))
        with patch.object(sh, "read_balances", side_effect=AssertionError("must not be called")), \
                self.assertLogs(sh.logger, "WARNING"):
            snap = sh.take_snapshot()
        self.assertEqual((snap["address"], snap["balances"], snap["feedback"]), (None, {}, None))

    def test_settings(self):
        with patch.dict(os.environ, {"SLACK_HOME_ETH_USD": "3178.50", "SLACK_HOME_CACHE_SECONDS": "1"}):
            self.assertEqual((sh.eth_usd(), sh.cache_seconds()), (Decimal("3178.50"), sh.MIN_REFRESH_SECONDS))
        for bad in ("abc", "-1", "0", "nan", "inf"):
            with patch.dict(os.environ, {"SLACK_HOME_ETH_USD": bad}), self.assertLogs(sh.logger, "WARNING"):
                self.assertIsNone(sh.eth_usd(), bad)
        self.assertTrue(sh.enabled())
        with patch.dict(os.environ, {"SLACK_HOME": "false"}):
            self.assertFalse(sh.enabled())


class HomeViewTests(Base):
    def view(self, snap=None, **kwargs):
        return sh.build_home(snap or snapshot(), team_id=TEAM, app_id=APP, now=NOW, **kwargs)

    def test_total_is_the_mainnet_stablecoins_only(self):
        view = self.view()
        self.assertEqual(view["type"], "home")
        header = next(b for b in view["blocks"] if b["type"] == "header")
        self.assertEqual(header["text"]["text"], "$1,214.50")
        text = text_of(view)
        for expected in ("USDC · Base\\n*$812.40*", "SBC · Radius\\n*$402.10*", "*0.0214 ETH*", "Paid in SBC",
                         "SBC · Radius Testnet 412.50", "USDC · Base Sepolia 18.20", "not counted in the total",
                         "*Wallet* 0x3f2a…9a2c", "Total value · stablecoins", "Stablecoins valued at $1.00", "Helpful\\n*18*", "Not helpful\\n*3*"):
            self.assertIn(expected, text)
        self.assertNotIn(ADDRESS[10:30], text.replace(f"/address/{ADDRESS}", ""))

    def test_eth_price_and_floor_are_operator_settings(self):
        self.assertNotIn("about $", text_of(self.view()))
        self.assertNotIn("is low", text_of(self.view()))
        with patch.dict(os.environ, {"SLACK_HOME_ETH_USD": "3000", "SLACK_HOME_ETH_FLOOR": "0.03"}):
            view = self.view()
        text = text_of(view)
        self.assertIn("0.0214 ETH (about $64.20)", text)
        self.assertIn("ETH on Base is low", text)
        self.assertIn("under the 0.03 ETH floor", text)
        self.assertEqual(next(b for b in view["blocks"] if b["type"] == "header")["text"]["text"], "$1,214.50")

    def test_spending_reflects_the_x402_settings(self):
        text = text_of(self.view())
        for expected in ("Per-call cap\\n*No cap*", "Spent today (UTC)\\n*$0.06*", "*Any public host*", "*Radius, Base*"):
            self.assertIn(expected, text)
        with patch.dict(os.environ, {"X402_MAX_PER_CALL": "1", "X402_ALLOWED_HOSTS": "grug402.dev, api.example.com",
                                     "X402_PAID_NETWORKS": "radius-testnet"}):
            text = text_of(self.view())
        for expected in ("*$1.00 max*", "*2 hosts*", "*Radius Testnet*", "Testnet only"):
            self.assertIn(expected, text)
        with patch.dict(os.environ, {"X402_PAID_NETWORKS": "radius-testnet,base-mainnet,radius-mainnet,base-sepolia"}):
            self.assertIn("*Base, Radius (+ 2 testnets)*", text_of(self.view()))
        with patch.dict(os.environ, {"X402_PAID_NETWORKS": "", "X402_ALLOWED_HOSTS": "", "X402_MAX_PER_CALL": "unlimited"}):
            text = text_of(self.view())
        self.assertIn("None, payments are off", text)
        self.assertIn("Approved hosts\\n*None*", text)

    def table(self, view):
        tables = [b for b in view["blocks"] if b["type"] == "table"]
        self.assertEqual(len(tables), 1)
        return tables[0]

    def test_recent_payments_are_a_table(self):
        view = self.view()
        table = self.table(view)
        cells = [[c.get("text") or c["elements"][0]["elements"][0] for c in r] for r in table["rows"]]
        self.assertEqual([c["text"] for c in cells[0]], ["Time", "Endpoint", "Chain", "Paid", "Status"])
        self.assertEqual(len(cells), 5)
        self.assertEqual(cells[1][0]["type"], "date")
        self.assertEqual(cells[1][0]["timestamp"], int(datetime(2026, 11, 20, 9, 12, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(cells[1][1:4], ["grug402.dev/weather", "Radius", "0.05 SBC"])
        self.assertEqual(cells[1][4], {"type": "link", "url": f"https://network.radiustech.xyz/tx/{TX}", "text": "Settled"})
        self.assertEqual(cells[2][1:], ["grug402.dev/joke", "Base", "0.01 USDC", "Settled"])
        self.assertEqual(cells[3][3:], ["2 SBC", "Refused, over the cap"])
        self.assertEqual(cells[4][4], "Quote only")
        self.assertEqual(table["column_settings"][3], {"align": "right"})
        self.assertTrue(all(len(r) == 5 for r in table["rows"]))
        text = text_of(view)
        self.assertNotIn(USER, text)  # who asked is model-written free text; it is not shown
        self.assertIn("Mainnet only · from the local attempt ledger, not chain-reconciled", text)
        self.assertNotIn("showing", text)
        many = snapshot(ledger=[row(str(i), ts=f"2026-11-20T09:{i:02d}:00+00:00") for i in range(12)])
        self.assertIn("showing 5 of 12", text_of(self.view(many)))
        self.assertEqual(len(self.table(self.view(many))["rows"]), 1 + sh.HOME_ROWS)

    def test_recent_payments_as_text_lines(self):
        view = self.view(table=False)
        self.assertFalse([b for b in view["blocks"] if b["type"] == "table"])
        lines = next(b for b in view["blocks"] if b["type"] == "section" and "grug402.dev/weather" in b["text"]["text"])
        self.assertIs(lines["text"]["verbatim"], True)
        text = text_of(view)
        self.assertIn("grug402.dev/weather · Radius · *0.05 SBC*", text)
        self.assertIn(f"<https://network.radiustech.xyz/tx/{TX}|Settled>", text)
        self.assertIn("grug402.dev/joke · Base · *0.01 USDC* · Settled", text)
        self.assertIn("*2 SBC* · Refused, over the cap", text)
        self.assertNotIn(USER, text)

    def test_endpoints_are_shortened(self):
        self.assertEqual(sh.short_endpoint("https://x402scan.com/api/x402/merchants/0xff30" + "ab" * 18 + "?q=1"),
                         "x402scan.com/api/x402/merchants/0xff30…")
        # the end of the path is what tells two calls apart, so it is the part that is kept
        self.assertEqual(sh.short_endpoint("https://api.example.com/v1/some/very/long/path/to/a/resource/weather"),
                         "api.example.com/…/to/a/resource/weather")
        stats = [sh.short_endpoint(f"https://www.x402scan.com/api/x402/merchants/0x{i}f30" + "ab" * 18 + "/stats")
                 for i in "ef"]
        self.assertEqual(stats, ["x402scan.com/…/merchants/0xef30…/stats", "x402scan.com/…/merchants/0xff30…/stats"])
        self.assertEqual(sh.short_endpoint("https://www.grug402.dev/weather"), "grug402.dev/weather")
        self.assertLessEqual(len(sh.short_endpoint("https://a.example/" + "p" * 300 + "/" + "q" * 300)), 40)
        self.assertEqual(sh.short_endpoint("https://grug402.dev/"), "grug402.dev")
        self.assertEqual(len(sh.short_endpoint("https://grug402.dev/" + "p" * 300)), 40)
        for bad in ("", None, "not a url", "http://[bad"):
            self.assertEqual(sh.short_endpoint(bad), "unknown endpoint")

    def test_ledger_text_cannot_inject_markup_or_links(self):
        hostile = snapshot(ledger=[row(
            "x", url="https://evil.example/<!channel>*bold*`code`", requested_by="<!here> <https://evil.example|click>",
            explorer_url="https://evil.example/tx/" + TX)])
        row_ = self.table(self.view(hostile))["rows"][1]
        # raw_text cells are shown as typed, and a link to anything but the network's explorer is dropped
        self.assertEqual(row_[1], {"type": "raw_text", "text": "evil.example/<!channel>*bold*`code`"})
        self.assertEqual(row_[4], {"type": "raw_text", "text": "Settled"})
        for view in (self.view(hostile), self.view(hostile, table=False)):
            self.assertNotIn("<!here>", text_of(view))
            self.assertNotIn("click", text_of(view))
            self.assertNotIn("evil.example/tx", text_of(view))
        text = text_of(self.view(hostile, table=False))
        self.assertNotIn("<!channel>", text)
        self.assertIn("evil.example/&lt;!channel&gt;boldcode", text)

    def test_buttons_and_wallet_menu(self):
        view = self.view()
        rows = [[e["action_id"] for e in b["elements"]] for b in view["blocks"] if b["type"] == "actions"]
        self.assertEqual(rows, [[sh.ACTION_REFRESH, sh.ACTION_FUND], [sh.ACTION_PAYMENTS, "slack_home_link_ask"]])
        buttons = {e["action_id"]: e for b in view["blocks"] if b["type"] == "actions" for e in b["elements"]}
        self.assertEqual(buttons["slack_home_link_ask"]["url"], f"slack://app?team={TEAM}&id={APP}&tab=messages")
        self.assertTrue(sh.ACTION_LINK.fullmatch("slack_home_link_ask"))
        self.assertNotIn("url", buttons[sh.ACTION_REFRESH])
        menu = next(b["accessory"] for b in view["blocks"] if b.get("accessory"))
        self.assertEqual((menu["type"], menu["action_id"]), ("overflow", sh.ACTION_WALLET))
        self.assertEqual([(o["text"]["text"], o["value"], o.get("url")) for o in menu["options"]], [
            ("Show full address", "address", None),
            ("Radius explorer", "radius-mainnet", f"https://network.radiustech.xyz/address/{ADDRESS}"),
            ("Base explorer", "base-mainnet", f"https://basescan.org/address/{ADDRESS}")])
        self.assertLessEqual(len(menu["options"]), 5)
        no_ids = sh.build_home(snapshot(), now=NOW)
        self.assertNotIn("slack_home_link_ask", text_of(no_ids))

    def test_the_payments_button_only_says_all_when_the_modal_holds_them_all(self):
        def label(count):
            rows = [row(str(i), ts=f"2026-11-20T09:{i % 60:02d}:00+00:00") for i in range(count)]
            view = self.view(snapshot(ledger=rows))
            return next(e["text"]["text"] for b in view["blocks"] if b["type"] == "actions"
                        for e in b["elements"] if e["action_id"] == sh.ACTION_PAYMENTS)
        self.assertEqual(label(sh.MODAL_ROWS), "All payments")
        self.assertEqual(label(sh.MODAL_ROWS + 1), f"Latest {sh.MODAL_ROWS} payments")

    def test_the_address_is_in_a_text_field_when_the_modal_can_be_submitted(self):
        sh._live["views"] = True
        for view in (sh.build_address_modal(ADDRESS), sh.build_fund_modal(ADDRESS)):
            field = next(b for b in view["blocks"] if b["type"] == "input")
            self.assertEqual(field["element"]["initial_value"], ADDRESS)
            self.assertEqual(field["element"]["type"], "plain_text_input")
            self.assertEqual((view["callback_id"], view["submit"]["text"]), (sh.VIEW_ADDRESS, "Done"))
            self.assertNotIn("```", text_of(view))
        for view in (sh.build_address_modal(None), sh.build_fund_modal(None)):
            self.assertNotIn("submit", view)  # no input block, so no submit button

    def test_missing_data_is_shown_as_unavailable_not_zero(self):
        partial = snapshot(balances={**BALANCES, ("radius-mainnet", "token"): None, ("base-mainnet", "native"): None},
                           feedback=None)
        view = self.view(partial)
        text = text_of(view)
        self.assertEqual(next(b for b in view["blocks"] if b["type"] == "header")["text"]["text"], "$812.40")
        self.assertIn("not included: SBC on Radius (could not be read)", text)
        self.assertIn("SBC · Radius\\n*Unavailable*", text)
        self.assertIn("ETH · Base\\n*Unavailable*", text)
        self.assertIn("_Unavailable._", text)
        none = self.view(snapshot(address=None, balances={}, ledger=[]))
        text = text_of(none)
        self.assertEqual(next(b for b in none["blocks"] if b["type"] == "header")["text"]["text"], "Unavailable")
        self.assertIn("wallet address could not be read", text)
        self.assertIn("No payments yet", text)
        self.assertNotIn(sh.ACTION_FUND, text)
        self.assertNotIn(sh.ACTION_WALLET, text)
        self.assertNotIn('"table"', text)
        self.assertNotIn("$0.00*\"", text.split("Spent today")[0])

    def test_views_fit_slack_limits(self):
        big = snapshot(ledger=[row(str(i), url="https://grug402.dev/" + "p" * 300, requested_by="n" * 300,
                                   ts=f"2026-11-20T09:{i % 60:02d}:00+00:00") for i in range(60)])
        home = self.view(big)
        modal = sh.build_payments_modal(sh.payments(big["ledger"], "mainnet"), "mainnet")
        table = next(b for b in home["blocks"] if b["type"] == "table")
        self.assertLessEqual(len(json.dumps(table["rows"])), 10000)
        self.assertNotIn('"table"', text_of(modal))  # Slack has no table block in modals
        for view in (home, self.view(big, table=False), modal, sh.build_fund_modal(ADDRESS),
                     sh.build_address_modal(ADDRESS), sh.build_limited_home(team_id=TEAM, app_id=APP)):
            self.assertLessEqual(len(view["blocks"]), 100)
            for block in view["blocks"]:
                if block["type"] == "section":
                    self.assertLessEqual(len(block["text"]["text"]), 3000)
                    self.assertLessEqual(len(block.get("fields", [])), 10)
                if block["type"] == "actions":
                    self.assertLessEqual(len(block["elements"]), 25)
        self.assertIn("latest 25 of 60", text_of(modal))
        for view in (modal, sh.build_fund_modal(ADDRESS), sh.build_address_modal(ADDRESS)):
            self.assertLessEqual(len(view["title"]["text"]), 24)

    def test_limited_view_and_fund_modal(self):
        limited = text_of(sh.build_limited_home(team_id=TEAM, app_id=APP))
        self.assertIn("only shown to people approved", limited)
        self.assertNotIn("$", limited)
        fund = text_of(sh.build_fund_modal(ADDRESS))
        self.assertIn(f"```{ADDRESS}```", fund)
        self.assertIn("Nothing on this tab can move funds", fund)
        self.assertIn("could not be read", text_of(sh.build_fund_modal(None)))
        self.assertIn(f"```{ADDRESS}```", text_of(sh.build_address_modal(ADDRESS)))
        self.assertIn("could not be read", text_of(sh.build_address_modal(None)))


class SlackTests(Base):
    def setUp(self):
        super().setUp()
        patcher = patch.object(sh, "read_balances", lambda address: dict(BALANCES))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_ledger(LEDGER)

    def opened(self, user=USER, tab="home"):
        return {"type": "event_callback", "team_id": TEAM, "api_app_id": APP,
                "event": {"type": "app_home_opened", "user": user, "tab": tab, "channel": "D0123456789"}}

    def click(self, action_id, user=USER, **action):
        return {"type": "block_actions", "user": {"id": user, "team_id": TEAM}, "team": {"id": TEAM},
                "api_app_id": APP, "trigger_id": "123.456.abc", "view": {"type": "home"},
                "actions": [{"action_id": action_id, "type": "button", **action}]}

    def run_middleware(self, body):
        passed = []

        async def go():
            async def next_():
                passed.append(True)
            await sh._home_middleware(body, next_)
            await asyncio.gather(*list(sh._tasks))
        asyncio.run(go())
        return passed

    def run_action(self, handler, body):
        acks = []

        async def go():
            async def ack():
                acks.append(True)
            await handler(ack, body, body["actions"][0])
            await asyncio.gather(*list(sh._tasks))
        asyncio.run(go())
        return acks

    def test_opening_the_home_tab_publishes_the_view(self):
        self.assertEqual(self.run_middleware(self.opened()), [True])
        published = self.app.client.published
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["user_id"], USER)
        self.assertIn("$1,214.50", text_of(published[0]["view"]))
        self.assertIn(f"slack://app?team={TEAM}&id={APP}", text_of(published[0]["view"]))
        self.assertEqual(self.adapter.asked, [(USER, "D0123456789", TEAM)])

    def test_other_events_pass_through_untouched(self):
        for body in (self.opened(tab="messages"), {"event": {"type": "message", "user": USER}}, {"type": "block_actions"},
                     None, self.opened(user="not a user")):
            self.assertEqual(self.run_middleware(body), [True])
        self.assertEqual(self.app.client.published, [])

    def test_people_who_are_not_approved_get_no_balances(self):
        self.run_middleware(self.opened(user="U999STRANGER"))
        view = text_of(self.app.client.published[0]["view"])
        self.assertIn("only shown to people approved", view)
        self.assertNotIn("$", view)
        self.assertEqual(self.tool_calls, [])
        sh._live["adapter"] = object()  # no way to check: fail closed
        self.run_middleware(self.opened())
        self.assertNotIn("$", text_of(self.app.client.published[1]["view"]))

    def test_a_slack_failure_never_breaks_dispatch(self):
        error = RuntimeError("boom xoxb-secret")
        error.response = {"ok": False, "error": "not_enabled"}
        self.app.client.fail = error
        with self.assertLogs(sh.logger, "WARNING") as logs:
            self.assertEqual(self.run_middleware(self.opened()), [True])
        self.assertIn("not_enabled", "".join(logs.output))
        self.assertNotIn("xoxb", "".join(logs.output))
        with patch.object(sh, "home_opened", side_effect=RuntimeError("bug")), self.assertLogs(sh.logger, "ERROR"):
            self.assertEqual(self.run_middleware(self.opened()), [True])

    def test_falls_back_to_text_lines_if_slack_refuses_the_table(self):
        error = RuntimeError("invalid")
        error.response = {"ok": False, "error": "invalid_arguments"}
        self.app.client.fail, self.app.client.fail_tables_only = error, True
        with self.assertLogs(sh.logger, "WARNING") as logs:
            self.run_middleware(self.opened())
        self.assertIn("refused the payments table (invalid_arguments)", "".join(logs.output))
        view = text_of(self.app.client.published[0]["view"])
        self.assertNotIn('"table"', view)
        self.assertIn("grug402.dev/weather · Radius", view)
        self.assertIs(sh._live["tables"], False)
        self.run_middleware(self.opened())  # no second refusal: the table is not tried again
        self.assertEqual(len(self.app.client.published), 2)

    def test_other_slack_errors_do_not_turn_tables_off(self):
        error = RuntimeError("limited")
        error.response = {"ok": False, "error": "ratelimited"}
        self.app.client.fail = error
        with self.assertLogs(sh.logger, "WARNING"):
            self.run_middleware(self.opened())
        self.assertEqual(self.app.client.published, [])
        self.assertIs(sh._live["tables"], True)

    def test_switched_off(self):
        with patch.dict(os.environ, {"SLACK_HOME": "false"}):
            self.assertEqual(self.run_middleware(self.opened()), [True])
            self.assertEqual(self.run_action(sh.on_refresh, self.click(sh.ACTION_REFRESH)), [True])
            self.assertEqual(self.run_action(sh.on_fund, self.click(sh.ACTION_FUND)), [True])
        self.assertEqual((self.app.client.published, self.app.client.opened), ([], []))

    def test_refresh_acks_then_republishes(self):
        self.assertEqual(self.run_action(sh.on_refresh, self.click(sh.ACTION_REFRESH)), [True])
        self.assertEqual(len(self.app.client.published), 1)

    def test_modals_open_for_approved_people_only(self):
        sh._cache["address"] = ADDRESS
        self.assertEqual(self.run_action(sh.on_fund, self.click(sh.ACTION_FUND)), [True])
        self.assertEqual(self.run_action(sh.on_payments, self.click(sh.ACTION_PAYMENTS)), [True])
        fund, pay = self.app.client.opened
        self.assertEqual(fund["trigger_id"], "123.456.abc")
        self.assertIn(ADDRESS, text_of(fund["view"]))
        self.assertIn("grug402.dev/weather", text_of(pay["view"]))
        self.assertNotIn("Radius Testnet", text_of(pay["view"]))
        self.run_action(sh.on_fund, self.click(sh.ACTION_FUND, user="U999STRANGER"))
        self.run_action(sh.on_payments, self.click(sh.ACTION_PAYMENTS, user="U999STRANGER"))
        self.assertEqual(len(self.app.client.opened), 2)
        self.assertEqual(self.run_action(sh.on_link, self.click("slack_home_link_ask")), [True])

    def test_wallet_menu(self):
        sh._cache["address"] = ADDRESS
        pick = lambda value, **kw: self.click(sh.ACTION_WALLET, selected_option={"value": value}, **kw)  # noqa: E731
        self.assertEqual(self.run_action(sh.on_wallet, pick("address")), [True])
        self.assertIn(f"```{ADDRESS}```", text_of(self.app.client.opened[0]["view"]))
        for body in (pick("base-mainnet"), pick("address", user="U999STRANGER"), self.click(sh.ACTION_WALLET)):
            self.assertEqual(self.run_action(sh.on_wallet, body), [True])
        self.assertEqual(len(self.app.client.opened), 1)

    def test_a_real_slack_app_acks_the_address_modal(self):
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.authorization import AuthorizeResult
        from slack_bolt.request.async_request import AsyncBoltRequest

        async def authorize(**_kwargs):
            return AuthorizeResult(enterprise_id=None, team_id=TEAM, bot_token="xoxb-test", bot_id="B1", bot_user_id="U1")

        async def go():
            app = AsyncApp(signing_secret="secret", authorize=authorize)
            self.assertTrue(sh.wire_slack(app, self.adapter))
            body = {"type": "view_submission", "team": {"id": TEAM}, "user": {"id": USER, "team_id": TEAM},
                    "api_app_id": APP, "view": {"type": "modal", "callback_id": sh.VIEW_ADDRESS,
                                                "state": {"values": {sh.VIEW_ADDRESS: {"address": {"value": "edited"}}}}}}
            return await app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode"))
        response = asyncio.run(go())
        self.assertEqual((response.status, response.body), (200, ""))

    def test_wiring_adds_the_middleware_once_per_app(self):
        app = FakeApp()
        self.assertTrue(sh.wire_slack(app, self.adapter))
        self.assertFalse(sh.wire_slack(app, self.adapter))
        self.assertEqual(app.middleware, [sh._home_middleware])
        self.assertEqual(app.views, {sh.VIEW_ADDRESS: sh.on_address_done})
        self.assertIs(sh._live["views"], True)
        self.assertIs(sh._live["app"], app)
        with self.assertLogs(sh.logger, "WARNING"):
            self.assertFalse(sh.wire_slack(object()))

    def test_a_payment_republishes_for_the_person_who_asked(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        sh._live["loop"] = loop
        sh._cache["snapshot"] = snapshot()
        session = {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_USER_ID": USER, "HERMES_SESSION_SCOPE_ID": TEAM}
        fake = type(sys)("gateway.session_context")
        fake.get_session_env = lambda name, default="": session.get(name, default)
        with patch.dict(sys.modules, {"gateway": type(sys)("gateway"), "gateway.session_context": fake}):
            sh.after_tool_call(tool_name="radius_balance", args={}, result="{}")
            self.assertIsNotNone(sh._cache["snapshot"])
            sh.after_tool_call(tool_name="x402_request", args={}, result="{}", task_id="t")
            session["HERMES_SESSION_PLATFORM"] = "telegram"
            sh.after_tool_call(tool_name="x402_request")
        for _ in range(200):
            if self.app.client.published:
                break
            threading.Event().wait(0.02)
        self.assertEqual([p["user_id"] for p in self.app.client.published], [USER])
        self.assertAlmostEqual(sh._cache["snapshot"]["taken_at"], datetime.now(timezone.utc).timestamp(), delta=30)


class RegistrationTests(Base):
    class Ctx:
        def __init__(self):
            self.factories, self.actions, self.hooks = [], [], []

        def register_platform_handler(self, platform, factory):
            self.factories.append((platform, factory))

        def register_slack_action_handler(self, action_id, callback):
            self.actions.append((action_id, callback))

        def register_hook(self, name, callback):
            self.hooks.append((name, callback))

        def dispatch_tool(self, name, args, **kwargs):
            return "{}"

    def test_registers_handlers_and_no_tools(self):
        ctx = self.Ctx()
        sh.register(ctx)
        self.assertEqual(ctx.factories, [("slack", sh.wire_slack)])
        self.assertEqual(dict(ctx.actions), {sh.ACTION_REFRESH: sh.on_refresh, sh.ACTION_FUND: sh.on_fund,
                                             sh.ACTION_PAYMENTS: sh.on_payments, sh.ACTION_WALLET: sh.on_wallet,
                                             sh.ACTION_LINK: sh.on_link})
        self.assertEqual(ctx.hooks, [("post_tool_call", sh.after_tool_call)])
        self.assertFalse(hasattr(ctx, "register_tool"))

    def test_off_or_on_an_old_hermes_it_registers_nothing(self):
        ctx = self.Ctx()
        with patch.dict(os.environ, {"SLACK_HOME": "false"}), self.assertLogs(sh.logger, "INFO"):
            sh.register(ctx)
        self.assertEqual((ctx.factories, ctx.actions, ctx.hooks), ([], [], []))
        with self.assertLogs(sh.logger, "WARNING"):
            sh.register(object())


class PinnedHermesContractTests(unittest.TestCase):
    """Runs only where the pinned Hermes is importable (the image, or a dev venv with it installed)."""

    def test_plugin_api_and_adapter_methods(self):
        try:
            import hermes_cli.plugins as hermes_plugins
            from plugins.platforms.slack.adapter import SlackAdapter
        except Exception as err:  # pragma: no cover - depends on the environment
            self.skipTest(f"pinned Hermes is not importable: {err}")
        for name in ("register_platform_handler", "register_slack_action_handler", "register_hook", "dispatch_tool"):
            self.assertTrue(callable(getattr(hermes_plugins.PluginContext, name, None)), name)
        self.assertIn("post_tool_call", hermes_plugins.VALID_HOOKS)
        self.assertTrue(callable(getattr(SlackAdapter, "_is_interactive_user_authorized", None)))
        self.assertTrue(callable(getattr(SlackAdapter, "_get_client", None)))


class RepoTests(unittest.TestCase):
    def test_manifest_enables_the_home_tab(self):
        manifest = json.loads((ROOT / "slack/manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["features"]["app_home"]["home_tab_enabled"])
        self.assertIn("app_home_opened", manifest["settings"]["event_subscriptions"]["bot_events"])

    def test_docs_name_the_settings(self):
        for name in ("README.md", ".env.example"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for var in ("SLACK_HOME", "SLACK_HOME_ETH_USD", "SLACK_HOME_ETH_FLOOR", "SLACK_HOME_CACHE_SECONDS"):
                self.assertIn(var, text, f"{var} in {name}")

    def test_the_plugin_cannot_move_funds(self):
        source = (ROOT / "plugins/slack-home/__init__.py").read_text(encoding="utf-8")
        for forbidden in ("eth_sendTransaction", "eth_sendRawTransaction", "eth_sign", "x402_request\",", "radius_send_sbc",
                          "wallet\", \"pay", "wallet\", \"send", "PRIVATE_KEY"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
