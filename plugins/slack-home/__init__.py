"""Slack App Home tab for the agent: wallet balances, recent x402 payments and feedback.

One Block Kit view per person, published with ``views.publish`` when they open the Home tab,
click Refresh, or the agent finishes an ``x402_request`` they asked for.

Recent payments are a ``table`` block. Slack allows tables on Home tabs but not in modals, so the
"All payments" modal lists them as text lines, and the Home tab falls back to the same lines if
Slack refuses the table.

Everything here is read-only. Balances come from ``eth_getBalance`` / ``balanceOf`` calls to
the chains' public RPC endpoints, payments from the x402 plugin's local ledger file, and feedback
from the ``feedback_ledger`` tool. Nothing on the tab can move funds.

Hermes is not patched. The plugin uses ``ctx.register_platform_handler("slack", ...)`` to add a
slack_bolt global middleware (Hermes already owns the one ``app_home_opened`` listener, and
slack_bolt runs only the first matching listener), ``ctx.register_slack_action_handler`` for the
buttons, and a ``post_tool_call`` hook.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Mirrors NETWORKS in plugins/x402; tests/test_slack_home.py fails if the two drift apart.
NETWORKS = {
    "radius-mainnet": {"network": "eip155:723487", "family": "radius", "environment": "mainnet", "label": "Radius",
                       "asset": "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb", "symbol": "SBC", "decimals": 6,
                       "rpc": "https://rpc.radiustech.xyz", "explorer": "https://network.radiustech.xyz"},
    "base-mainnet": {"network": "eip155:8453", "family": "base", "environment": "mainnet", "label": "Base",
                     "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "symbol": "USDC", "decimals": 6,
                     "rpc": "https://mainnet.base.org", "explorer": "https://basescan.org"},
    "radius-testnet": {"network": "eip155:72344", "family": "radius", "environment": "testnet",
                       "label": "Radius Testnet",
                       "asset": "0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb", "symbol": "SBC", "decimals": 6,
                       "rpc": "https://rpc.testnet.radiustech.xyz", "explorer": "https://testnet.radiustech.xyz"},
    "base-sepolia": {"network": "eip155:84532", "family": "base", "environment": "testnet", "label": "Base Sepolia",
                     "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "symbol": "USDC", "decimals": 6,
                     "rpc": "https://sepolia.base.org", "explorer": "https://sepolia.basescan.org"},
}
BY_CAIP = {spec["network"]: name for name, spec in NETWORKS.items()}
GAS_NETWORK = "base-mainnet"  # the one chain where gas is paid in ETH; Radius gas is paid in SBC

ACTION_REFRESH = "slack_home_refresh"
ACTION_FUND = "slack_home_fund"
ACTION_PAYMENTS = "slack_home_payments"
ACTION_WALLET = "slack_home_wallet"  # the overflow menu beside the wallet address
VIEW_ADDRESS = "slack_home_address"  # callback id of the modals that hold the address in a text field
ACTION_LINK = re.compile(r"slack_home_link_\w+")  # link buttons still send a click that needs an ack

RPC_TIMEOUT = 6
HOME_ROWS = 5
MODAL_ROWS = 25
MIN_REFRESH_SECONDS = 5
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_SLACK_USER = re.compile(r"[UW][A-Z0-9]{6,20}")
OUTCOMES = {
    "settlement_reported": "Settled", "payment_unknown": "Unknown, check the wallet", "refused": "Refused, over the cap",
    "quoted": "Quote only", "pending": "Pending", "error": "Failed",
}

# The live Slack app and the snapshot cache. Process-local, like the gateway itself.
_live: dict = {"app": None, "adapter": None, "loop": None, "dispatch": None, "tables": True, "views": False}
_cache: dict = {"snapshot": None, "address": None}
_cache_lock = threading.Lock()
_dm_channels: dict = {}  # Slack user id -> their DM channel with the app, for the authorization check
_tasks: set = set()


# --------------------------------------------------------------------------- settings

def enabled() -> bool:
    return os.environ.get("SLACK_HOME", "true").strip().lower() not in {"false", "0", "no", "off"}


def _positive(name: str) -> Decimal | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value <= 0:
        logger.warning("[slack-home] %s is not a positive number; ignoring it", name)
        return None
    return value


def eth_usd() -> Decimal | None:
    """Operator-supplied ETH price in USD. There is no price feed; unset means no USD value."""
    return _positive("SLACK_HOME_ETH_USD")


def eth_floor() -> Decimal | None:
    return _positive("SLACK_HOME_ETH_FLOOR")


def cache_seconds() -> int:
    value = _positive("SLACK_HOME_CACHE_SECONDS")
    return max(MIN_REFRESH_SECONDS, min(3600, int(value))) if value else 60


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/data/.hermes"))


def ledger_path() -> Path:
    return Path(os.environ.get("X402_LEDGER_PATH") or _hermes_home() / "x402" / "ledger.jsonl")


def paid_networks() -> list[str]:
    names = []
    for raw in os.environ.get("X402_PAID_NETWORKS", "radius-testnet").split(","):
        name = {"base-testnet": "base-sepolia"}.get(raw.strip().lower(), raw.strip().lower())
        name = BY_CAIP.get(name, name)
        if name in NETWORKS and name not in names:
            names.append(name)
    return names


def payment_environment() -> str:
    """Payments shown are mainnet ones whenever a mainnet network is enabled for paying."""
    return "mainnet" if any(NETWORKS[n]["environment"] == "mainnet" for n in paid_networks()) else "testnet"


def template_version() -> str:
    for path in (Path("/app/VERSION"), Path(__file__).resolve().parents[2] / "VERSION"):
        try:
            return path.read_text(encoding="utf-8").strip()[:20]
        except OSError:
            continue
    return ""


# --------------------------------------------------------------------------- reading

def _rpc(rpc: str, method: str, params: list) -> int:
    import requests

    response = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                             timeout=RPC_TIMEOUT)
    result = response.json().get("result")
    if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", result):
        raise ValueError("no result")
    return int(result, 16)


def _read_one(name: str, kind: str, address: str) -> Decimal | None:
    spec = NETWORKS[name]
    try:
        if kind == "native":
            return Decimal(_rpc(spec["rpc"], "eth_getBalance", [address, "latest"])) / Decimal(10**18)
        data = "0x70a08231" + address[2:].lower().rjust(64, "0")  # balanceOf(address)
        raw = _rpc(spec["rpc"], "eth_call", [{"to": spec["asset"], "data": data}, "latest"])
        return Decimal(raw) / Decimal(10 ** spec["decimals"])
    except Exception as err:
        logger.warning("[slack-home] %s %s balance unavailable: %s", name, kind, type(err).__name__)
        return None


def read_balances(address: str) -> dict:
    """``{(network name, "token" | "native"): Decimal | None}``; None means the read failed."""
    wanted = [(name, "token") for name in NETWORKS] + [(GAS_NETWORK, "native")]
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        values = list(pool.map(lambda item: _read_one(*item, address), wanted))
    return dict(zip(wanted, values))


def read_ledger(path: Path | None = None) -> list[dict]:
    """The latest row of every payment attempt, oldest first (several rows share an ``id``)."""
    latest: dict = {}
    try:
        with (path or ledger_path()).open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    latest[row["id"]] = row
    except OSError:
        return []
    return list(latest.values())


def _when(row: dict) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(row.get("ts")))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _amount(row: dict) -> Decimal | None:
    try:
        value = Decimal(str(row.get("amount")))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def payments(rows: list[dict], environment: str) -> list[dict]:
    """Attempts that got as far as a priced offer on a network in ``environment``, newest first."""
    keep = []
    for row in rows:
        name = BY_CAIP.get(row.get("network"))
        if name and NETWORKS[name]["environment"] == environment and row.get("outcome") in OUTCOMES \
                and _when(row) and _amount(row) is not None:
            keep.append(row)
    return sorted(keep, key=_when, reverse=True)


def spent_today(rows: list[dict], now: datetime) -> Decimal:
    return sum((_amount(row) for row in rows
                if row.get("outcome") == "settlement_reported" and _when(row).astimezone(timezone.utc).date() == now.date()),
               Decimal(0))


def _tool(name: str, args: dict) -> dict:
    dispatch = _live["dispatch"]
    if not callable(dispatch):
        return {}
    try:
        result = dispatch(name, args)
        result = json.loads(result) if isinstance(result, str) else result
        return result if isinstance(result, dict) else {}
    except Exception as err:
        logger.warning("[slack-home] %s failed: %s", name, type(err).__name__)
        return {}


def wallet_address() -> str | None:
    if _cache["address"]:
        return _cache["address"]
    address = str(_tool("radius_wallet_address", {}).get("address") or "")
    if _ADDRESS.fullmatch(address):
        _cache["address"] = address
        return address
    return None


def feedback_counts() -> dict | None:
    summary = _tool("feedback_ledger", {"since": "7d", "limit": 1}).get("summary")
    if isinstance(summary, dict) and all(type(summary.get(key)) is int for key in ("good", "bad")):
        return {"good": summary["good"], "bad": summary["bad"]}
    return None


def take_snapshot(force: bool = False) -> dict:
    """Everything the tab shows. Blocking (subprocess, HTTP, files): call it from a worker thread."""
    with _cache_lock:
        cached = _cache["snapshot"]
        age = time.time() - cached["taken_at"] if cached else None
        if cached and age < (MIN_REFRESH_SECONDS if force else cache_seconds()):
            return cached
        address = wallet_address()
        snapshot = {
            "taken_at": time.time(), "address": address,
            "balances": read_balances(address) if address else {},
            "ledger": read_ledger(), "feedback": feedback_counts(),
        }
        _cache["snapshot"] = snapshot
        return snapshot


# --------------------------------------------------------------------------- formatting

def usd(value: Decimal) -> str:
    return f"${value:,.2f}"


def _text(value, limit: int = 60) -> str:
    """Untrusted text as one inert mrkdwn fragment."""
    text = " ".join(str(value or "").split())
    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(char, entity)
    text = re.sub(r"[`*_~]", "", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _date(stamp: float, style: str = "{time}") -> str:
    """A Slack date token, shown in the reader's own time zone."""
    fallback = datetime.fromtimestamp(stamp, timezone.utc).strftime("%b %d %H:%M UTC")
    return f"<!date^{int(stamp)}^{style}|{fallback}>"


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def _mrkdwn(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _section(text: str, fields: list[str] | None = None, *, verbatim: bool = False,
             accessory: dict | None = None) -> dict:
    block: dict = {"type": "section", "text": _mrkdwn(text)}
    if verbatim:  # stops Slack turning a bare host/path into a link
        block["text"]["verbatim"] = True
    if fields:
        block["fields"] = [_mrkdwn(field) for field in fields]
    if accessory:
        block["accessory"] = accessory
    return block


def _context(text: str) -> dict:
    return {"type": "context", "elements": [_mrkdwn(text)]}


def _button(text: str, action_id: str, *, url: str | None = None, primary: bool = False) -> dict:
    button: dict = {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": text}}
    if url:
        button["url"] = url
    if primary:
        button["style"] = "primary"
    return button


def short_endpoint(url, limit: int = 40) -> str:
    """``host/path`` without ``www.``, long hex ids cut to ``0x1234…`` and, if still long, the start of the
    path dropped. The end of a path is kept because it is what tells two calls to one seller apart."""
    try:
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").removeprefix("www.")
    except ValueError:
        host = ""
    if not host:
        return "unknown endpoint"
    path = re.sub(r"0x([0-9a-fA-F]{4})[0-9a-fA-F]{5,}", "0x\\1…", parsed.path if parsed.path != "/" else "")
    text = " ".join((host + path).split())
    parts = " ".join(path.split()).strip("/").split("/")
    if len(text) > limit and len(parts) > 1:
        kept: list[str] = []
        for part in reversed(parts[1:]):
            if len("/".join([host, "…", part, *kept])) > limit:
                break
            kept.insert(0, part)
        if kept:
            text = "/".join([host, "…", *kept])
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _paid(row: dict, spec: dict) -> str:
    return f"{_amount(row).normalize():f} {spec['symbol']}"


def _receipt(row: dict, spec: dict) -> str | None:
    """The explorer link of a settled payment, only when it points at that network's own explorer."""
    link = str(row.get("explorer_url") or "")
    if row["outcome"] == "settlement_reported" and re.fullmatch(re.escape(spec["explorer"]) + r"/tx/0x[0-9a-fA-F]{64}", link):
        return link
    return None


def payment_line(row: dict) -> str:
    spec = NETWORKS[BY_CAIP[row["network"]]]
    status, link = OUTCOMES[row["outcome"]], _receipt(row, spec)
    return " · ".join([_date(_when(row).timestamp(), "{date_short_pretty} {time}"), _text(short_endpoint(row.get("url"))),
                       spec["label"], f"*{_paid(row, spec)}*", f"<{link}|{status}>" if link else status])


def _payment_blocks(rows: list[dict]) -> list[dict]:
    # A section holds 3000 characters; five rows stay well under it.
    return [_section("\n".join(payment_line(row) for row in rows[i:i + 5]), verbatim=True)
            for i in range(0, len(rows), 5)]


def _raw(text: str) -> dict:
    return {"type": "raw_text", "text": text}  # shown as typed: no mrkdwn, no auto-links


def _rich(element: dict) -> dict:
    return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [element]}]}


def payments_table(rows: list[dict]) -> dict:
    """A ``table`` block: Time, Endpoint, Chain, Paid, Status. Keep it small; a table holds 10,000 characters."""
    table = [[_rich({"type": "text", "text": title, "style": {"bold": True}})
              for title in ("Time", "Endpoint", "Chain", "Paid", "Status")]]
    for row in rows:
        spec = NETWORKS[BY_CAIP[row["network"]]]
        stamp, status, link = _when(row), OUTCOMES[row["outcome"]], _receipt(row, spec)
        table.append([
            _rich({"type": "date", "timestamp": int(stamp.timestamp()), "format": "{date_short_pretty} {time}",
                   "fallback": stamp.astimezone(timezone.utc).strftime("%b %d %H:%M UTC")}),
            _raw(short_endpoint(row.get("url"))),
            _raw(spec["label"]),
            _raw(_paid(row, spec)),
            _rich({"type": "link", "url": link, "text": status}) if link else _raw(status),
        ])
    return {"type": "table", "rows": table, "column_settings": [
        {"align": "left"}, {"align": "left"}, {"align": "left"}, {"align": "right"}, {"align": "left"}]}


def messages_tab_url(team_id: str, app_id: str) -> str | None:
    if re.fullmatch(r"T[A-Z0-9]+", team_id or "") and re.fullmatch(r"A[A-Z0-9]+", app_id or ""):
        return f"slack://app?team={team_id}&id={app_id}&tab=messages"
    return None


# --------------------------------------------------------------------------- views

def build_home(snapshot: dict, *, team_id: str = "", app_id: str = "", now: datetime | None = None,
               table: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    address, balances = snapshot.get("address"), snapshot.get("balances") or {}
    blocks: list[dict] = []

    stable = {name: balances.get((name, "token")) for name in ("base-mainnet", "radius-mainnet")}
    known = [value for value in stable.values() if value is not None]
    missing = [f"{NETWORKS[n]['symbol']} on {NETWORKS[n]['label']}" for n, v in stable.items() if v is None]
    total = usd(sum(known, Decimal(0))) if known else "Unavailable"
    blocks.append(_context("Total value · stablecoins"))
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": total}})
    note = "Mainnet · Radius and Base · updated " + _date(snapshot["taken_at"])
    if not address:
        note = "The wallet address could not be read, so no balances are shown."
    elif missing and known:
        note += " · not included: " + ", ".join(missing) + " (could not be read)"
    blocks.append(_context(note))

    def money(name: str) -> str:
        value = stable[name]
        return f"{NETWORKS[name]['symbol']} · {NETWORKS[name]['label']}\n*{usd(value) if value is not None else 'Unavailable'}*"

    if address:
        menu = [{"text": {"type": "plain_text", "text": "Show full address"}, "value": "address"}]
        menu += [{"text": {"type": "plain_text", "text": f"{NETWORKS[name]['label']} explorer"}, "value": name,
                  "url": f"{NETWORKS[name]['explorer']}/address/{address}"} for name in ("radius-mainnet", "base-mainnet")]
        blocks.append(_section(f"*Wallet* {_short(address)}",
                               accessory={"type": "overflow", "action_id": ACTION_WALLET, "options": menu}))
        blocks.append(_section("*Stablecoins*", [money("base-mainnet"), money("radius-mainnet")]))
        eth, price, floor = balances.get((GAS_NETWORK, "native")), eth_usd(), eth_floor()
        eth_text = "Unavailable" if eth is None else f"{eth:.4f} ETH"
        if eth is not None and price:
            eth_text += f" (about {usd(eth * price)})"
        blocks.append(_section("*ETH for gas*", [f"ETH · Base\n*{eth_text}*", "Gas on Radius\n*Paid in SBC*"]))
        if eth is not None and floor and eth < floor:
            blocks.append(_section(f":warning: *ETH on Base is low.* {eth:.4f} ETH, under the {floor.normalize():f} ETH "
                                   "floor. Base payments fail once gas runs out."))
        blocks.append({"type": "actions", "elements": [
            _button("Refresh", ACTION_REFRESH, primary=True),
            _button("Fund wallet", ACTION_FUND),
        ]})
    else:
        blocks.append({"type": "actions", "elements": [_button("Refresh", ACTION_REFRESH, primary=True)]})

    environment = payment_environment()
    rows = payments(snapshot.get("ledger") or [], environment)
    cap = _positive("X402_MAX_PER_CALL") if os.environ.get("X402_MAX_PER_CALL", "").strip().lower() != "unlimited" else None
    hosts = [h for h in (s.strip() for s in os.environ.get("X402_ALLOWED_HOSTS", "").split(",")) if h]
    host_text = "Any public host" if "*" in hosts else f"{len(hosts)} host{'s' if len(hosts) != 1 else ''}" if hosts else "None"
    paid = {env: [NETWORKS[n]["label"] for n in paid_networks() if NETWORKS[n]["environment"] == env]
            for env in ("mainnet", "testnet")}
    networks = ", ".join(paid["mainnet"] or paid["testnet"]) or "None, payments are off"
    if paid["mainnet"] and paid["testnet"]:
        networks += f" (+ {len(paid['testnet'])} testnet{'s' if len(paid['testnet']) != 1 else ''})"
    blocks.append({"type": "divider"})
    blocks.append(_section("*Spending*", [
        f"Per-call cap\n*{usd(cap) + ' max' if cap else 'No cap'}*",
        f"Spent today (UTC)\n*{usd(spent_today(rows, now))}*",
        f"Approved hosts\n*{host_text}*",
        f"Pays on\n*{networks}*",
    ]))

    blocks.append({"type": "divider"})
    blocks.append(_section("*Recent x402 payments*"))
    if not rows:
        blocks.append(_section("_No payments yet._"))
    elif table:
        blocks.append(payments_table(rows[:HOME_ROWS]))
    else:
        blocks.extend(_payment_blocks(rows[:HOME_ROWS]))
    caption = f"{'Mainnet' if environment == 'mainnet' else 'Testnet'} only · from the local attempt ledger, not chain-reconciled"
    if len(rows) > HOME_ROWS:
        caption += f" · showing {HOME_ROWS} of {len(rows)}"
    blocks.append(_context(caption))
    # The modal holds MODAL_ROWS payments, so the button only says "All" when that is all of them.
    more = f"Latest {MODAL_ROWS} payments" if len(rows) > MODAL_ROWS else "All payments"
    actions = [_button(more, ACTION_PAYMENTS)] if rows else []
    ask = messages_tab_url(team_id, app_id)
    if ask:
        actions.append(_button("Ask the agent", "slack_home_link_ask", url=ask))
    if actions:
        blocks.append({"type": "actions", "elements": actions})

    feedback = snapshot.get("feedback")
    blocks.append({"type": "divider"})
    if feedback:
        blocks.append(_section("*Feedback, last 7 days*",
                               [f"Helpful\n*{feedback['good']}*", f"Not helpful\n*{feedback['bad']}*"]))
    else:
        blocks.append(_section("*Feedback, last 7 days*\n_Unavailable._"))

    if address:
        testnet = [f"{NETWORKS[n]['symbol']} · {NETWORKS[n]['label']} "
                   + (f"{balances[(n, 'token')]:,.2f}" if balances.get((n, "token")) is not None else "unavailable")
                   for n in ("radius-testnet", "base-sepolia")]
        blocks.append({"type": "divider"})
        blocks.append(_context("*Testnet · not real money* · " + " · ".join(testnet) + " · not counted in the total"))
    version = template_version()
    blocks.append(_context(" · ".join(filter(None, [
        f"Template {version}" if version else "", "Stablecoins valued at $1.00", "ETH is not in the total"]))))
    return {"type": "home", "blocks": blocks}


def build_limited_home(*, team_id: str = "", app_id: str = "") -> dict:
    """For people who are not allowed to talk to the agent: no balances, no payments."""
    blocks = [_section("*Radius Agent*\nWallet balances and payments on this tab are only shown to people "
                       "approved to use this agent.")]
    ask = messages_tab_url(team_id, app_id)
    if ask:
        blocks.append({"type": "actions", "elements": [_button("Open messages", "slack_home_link_ask", url=ask)]})
    return {"type": "home", "blocks": blocks}


def _modal(title: str, blocks: list[dict], *, callback_id: str | None = None) -> dict:
    view = {"type": "modal", "title": {"type": "plain_text", "text": title},
            "close": {"type": "plain_text", "text": "Close"}, "blocks": blocks}
    if callback_id:  # a modal with an input block must have a submit button, and its submission an ack
        view.update(callback_id=callback_id, submit={"type": "plain_text", "text": "Done"})
    return view


def _address_blocks(address: str) -> tuple[list[dict], str | None]:
    """The address where it can be copied, and the modal's callback id if it needs one.

    Slack's mobile apps don't let people select text in a view, code blocks included, but they do in a
    text field. So the address goes in a text input when the plugin can ack the modal's submission.
    """
    if not _live["views"]:
        return [_section(f"```{address}```")], None
    return [{"type": "input", "block_id": VIEW_ADDRESS, "optional": True,
             "label": {"type": "plain_text", "text": "Address"},
             "element": {"type": "plain_text_input", "action_id": "address", "initial_value": address},
             "hint": {"type": "plain_text", "text": "Select it to copy. Editing it here changes nothing."}}], VIEW_ADDRESS


def build_fund_modal(address: str | None) -> dict:
    if not address:
        return _modal("Fund wallet", [_section("The wallet address could not be read. Try Refresh.")])
    field, callback_id = _address_blocks(address)
    return _modal("Fund wallet", [
        _section("Send funds to the agent's address. It is the same address on every network."),
        *field,
        _section("*What to send*", [
            "USDC on Base\nfor Base payments", "SBC on Radius\nfor Radius payments and gas",
            "ETH on Base\nfor Base gas", "Testnet tokens\nnot real money"]),
        _context("Check the network before you send: funds sent on another chain are not shown here. "
                 "Nothing on this tab can move funds."),
    ], callback_id=callback_id)


def build_address_modal(address: str | None) -> dict:
    if not address:
        return _modal("Wallet address", [_section("The wallet address could not be read. Try Refresh.")])
    field, callback_id = _address_blocks(address)
    return _modal("Wallet address", [*field, _context("The same address on every network.")], callback_id=callback_id)


def build_payments_modal(rows: list[dict], environment: str) -> dict:
    shown = rows[:MODAL_ROWS]
    blocks = _payment_blocks(shown) if shown else [_section("_No payments yet._")]
    caption = f"{'Mainnet' if environment == 'mainnet' else 'Testnet'} only · not chain-reconciled"
    if len(rows) > len(shown):
        caption += f" · latest {len(shown)} of {len(rows)}; ask the agent for older ones"
    return _modal("x402 payments", [*blocks, _context(caption)])


# --------------------------------------------------------------------------- Slack

def _client(team_id: str = ""):
    app, adapter = _live["app"], _live["adapter"]
    get_client = getattr(adapter, "_get_client", None)  # multi-workspace installs
    if callable(get_client):
        try:
            client = get_client("", team_id=team_id or None)
            if getattr(client, "views_publish", None) is not None:
                return client
        except Exception as err:
            logger.debug("[slack-home] workspace client lookup failed: %s", type(err).__name__)
    return app.client


def authorized(user_id: str, team_id: str = "") -> bool:
    """Whether this person may talk to the agent, by Hermes' own check. Unknown means no."""
    check = getattr(_live["adapter"], "_is_interactive_user_authorized", None)
    if not callable(check):
        return False
    try:
        return bool(check(user_id, channel_id=_dm_channels.get(user_id, ""), team_id=team_id))
    except Exception as err:
        logger.warning("[slack-home] authorization check failed: %s", type(err).__name__)
        return False


def _error_code(err: Exception) -> str:
    response = getattr(err, "response", None)
    code = response.get("error") if response is not None and hasattr(response, "get") else None
    return code if isinstance(code, str) else ""


async def publish(user_id: str, team_id: str = "", app_id: str = "", *, force: bool = False) -> bool:
    """Build this person's view and publish it. Never raises."""
    try:
        if not authorized(user_id, team_id):
            view = build_limited_home(team_id=team_id, app_id=app_id)
        else:
            snapshot = await asyncio.get_running_loop().run_in_executor(None, take_snapshot, force)
            view = build_home(snapshot, team_id=team_id, app_id=app_id, table=_live["tables"])
        try:
            await _client(team_id).views_publish(user_id=user_id, view=view)
        except Exception as err:
            if not any(block["type"] == "table" for block in view["blocks"]) or not _error_code(err).startswith("invalid"):
                raise
            # Slack refused the view and it holds a table: publish the text-line version instead.
            view = build_home(snapshot, team_id=team_id, app_id=app_id, table=False)
            await _client(team_id).views_publish(user_id=user_id, view=view)
            _live["tables"] = False
            logger.warning("[slack-home] Slack refused the payments table (%s); using text lines from now on",
                           _error_code(err))
        return True
    except Exception as err:
        logger.warning("[slack-home] could not publish the Home tab: %s", _error_code(err) or type(err).__name__)
        return False


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _ids(body: dict) -> tuple[str, str, str]:
    """``(user id, team id, app id)`` from an event or interaction payload."""
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    team = body.get("team") if isinstance(body.get("team"), dict) else {}
    user_id = str(event.get("user") or user.get("id") or "")
    team_id = str(body.get("team_id") or team.get("id") or user.get("team_id") or "")
    return user_id, team_id, str(body.get("api_app_id") or "")


def home_opened(body) -> bool:
    """Start a publish when ``body`` is someone opening the Home tab. Returns whether it was."""
    if not isinstance(body, dict) or not isinstance(body.get("event"), dict):
        return False
    event = body["event"]
    if event.get("type") != "app_home_opened" or event.get("tab") != "home":
        return False
    user_id, team_id, app_id = _ids(body)
    if not _SLACK_USER.fullmatch(user_id):
        return False
    if isinstance(event.get("channel"), str):
        _dm_channels[user_id] = event["channel"]
    _spawn(publish(user_id, team_id, app_id))
    return True


async def _home_middleware(body, next):  # noqa: A002 - slack_bolt injects ``next`` by name
    """Global slack_bolt middleware: notice Home tab opens, always pass the request on."""
    try:
        if enabled():
            home_opened(body)
    except Exception:
        logger.exception("[slack-home] could not handle a Home tab open; Slack handling continues")
    finally:
        await next()
    return None


def wire_slack(app, adapter=None) -> bool:
    """``register_platform_handler("slack", ...)`` factory: add the middleware once per app."""
    use = getattr(app, "use", None)
    if not callable(use) or getattr(getattr(app, "client", None), "views_publish", None) is None:
        logger.warning("[slack-home] Slack app has no middleware or views API; the Home tab will stay empty")
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    _live.update(app=app, adapter=adapter, loop=loop)
    if getattr(app, "_radius_slack_home_wired", False):
        return False
    use(_home_middleware)
    view = getattr(app, "view", None)
    if callable(view):  # Hermes registers no view listeners, so this callback id is ours alone
        view(VIEW_ADDRESS)(on_address_done)
    _live["views"] = callable(view)
    try:
        app._radius_slack_home_wired = True
    except Exception:  # pragma: no cover - slotted app objects
        pass
    logger.info("[slack-home] publishing the App Home tab")
    return True


async def on_refresh(ack, body, action):
    await ack()
    user_id, team_id, app_id = _ids(body)
    if enabled() and _SLACK_USER.fullmatch(user_id):
        _spawn(publish(user_id, team_id, app_id, force=True))


async def _open_modal(body: dict, build) -> None:
    """Open a modal for an approved person. The trigger id is only good for three seconds."""
    user_id, team_id, _app_id = _ids(body)
    trigger = body.get("trigger_id")
    if not enabled() or not isinstance(trigger, str) or not authorized(user_id, team_id):
        return
    try:
        await _client(team_id).views_open(trigger_id=trigger, view=build())
    except Exception as err:
        logger.warning("[slack-home] could not open a modal: %s", type(err).__name__)


async def on_fund(ack, body, action):
    await ack()
    await _open_modal(body, lambda: build_fund_modal(_cache["address"]))


async def on_payments(ack, body, action):
    await ack()
    environment = payment_environment()
    rows = payments(await asyncio.get_running_loop().run_in_executor(None, read_ledger), environment)
    await _open_modal(body, lambda: build_payments_modal(rows, environment))


async def on_wallet(ack, body, action):
    """The wallet menu. The explorer options are links, which Slack opens itself."""
    await ack()
    selected = action.get("selected_option") if isinstance(action, dict) else None
    if isinstance(selected, dict) and selected.get("value") == "address":
        await _open_modal(body, lambda: build_address_modal(_cache["address"]))


async def on_address_done(ack):
    """The Done button on a modal that shows the address. Nothing is read from it; edits are dropped."""
    await ack()


async def on_link(ack, body, action):
    await ack()


def after_tool_call(tool_name: str = "", **_kwargs) -> None:
    """``post_tool_call`` hook: after a payment, refresh the tab of the person who asked for it."""
    if tool_name != "x402_request" or not enabled():
        return
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return
    loop = _live["loop"]
    user_id = str(get_session_env("HERMES_SESSION_USER_ID", "") or "")
    if str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").lower() != "slack" or loop is None \
            or not loop.is_running() or not _SLACK_USER.fullmatch(user_id):
        return
    team_id = str(get_session_env("HERMES_SESSION_SCOPE_ID", "") or "")
    _cache["snapshot"] = None  # the balances just changed; no lock, so a slow read can't stall the hook
    asyncio.run_coroutine_threadsafe(publish(user_id, team_id), loop)


def register(ctx):
    if not enabled():
        logger.info("[slack-home] SLACK_HOME is off; the App Home tab is not published")
        return
    needed = ("register_platform_handler", "register_slack_action_handler", "register_hook", "dispatch_tool")
    missing = [name for name in needed if not callable(getattr(ctx, name, None))]
    if missing:
        logger.warning("[slack-home] this Hermes lacks %s; the App Home tab is not published", ", ".join(missing))
        return
    _live["dispatch"] = ctx.dispatch_tool
    ctx.register_platform_handler("slack", wire_slack)
    ctx.register_slack_action_handler(ACTION_REFRESH, on_refresh)
    ctx.register_slack_action_handler(ACTION_FUND, on_fund)
    ctx.register_slack_action_handler(ACTION_PAYMENTS, on_payments)
    ctx.register_slack_action_handler(ACTION_WALLET, on_wallet)
    ctx.register_slack_action_handler(ACTION_LINK, on_link)
    ctx.register_hook("post_tool_call", after_tool_call)
