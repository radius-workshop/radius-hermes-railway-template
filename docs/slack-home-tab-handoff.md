# Handoff: Slack App Home tab dashboard (`slack-home` plugin)

You're building a Slack **App Home tab** for the Radius Agent: a Block Kit page that shows the agent's wallet, recent x402 payments and feedback. Build it as a bundled plugin in this repo. **Don't patch or fork Hermes.**

Read `AGENTS.md` first. It covers Python 3.11, the pre-finish checks, image pins and where runtime behaviour belongs.

- Design (visual target): https://claude.ai/artifact/H1qECNXtsUjyCji4KhNJpo. It has a mobile board, a desktop board and a block map. Every number on it is sample data.
- Pinned Hermes: `HERMES_GIT_REF=v2026.9.24` (`Dockerfile`). File and line references to Hermes below are from that tag and came from a read-only research pass. Re-check each one before relying on it.

---

## 0. Decide before you write code

1. **Mainnet or testnet data.** The design leads with a mainnet USD total: USDC on Base, SBC on Radius, and ETH on Base for gas, with testnet balances last. The code in this repo can't produce those numbers today:
   - `radius_balance` (`plugins/radius-cast/__init__.py`, around lines 241-247) returns RUSD (native) and SBC only.
   - `x402_wallet_status` (`plugins/x402/__init__.py`, around lines 473-477) pays only with SBC on Radius Testnet. Base and mainnet are disabled.
   - The operator runs this agent with real money. Ask them which branch or config the live deployment runs. If mainnet support exists elsewhere, read from that. If it doesn't, Phase 2 below adds **read-only** mainnet balance lookups. Never add or enable payment execution as part of this work.
2. **ETH price source** for the USD value. There's none in the repo. Don't invent one. Leave a clear TODO, or make it a setting the operator fills in.
3. **Which tokens and chains count in the total.** The default assumption is USDC on Base and SBC on Radius, with stablecoins valued at $1.00.

If the operator isn't available, build Phase 1 (works with today's data) and stop.

---

## 1. What already exists (verified)

| Need | Where | Notes |
|---|---|---|
| Plugin pattern to copy | `plugins/feedback-ledger/__init__.py` | `register(ctx)` at about line 631. `wire_slack(app, _adapter)` adds global slack_bolt middleware with `app.use(...)` and an idempotency flag (lines 413-440). Tests are in `tests/test_feedback_ledger.py`, loaded through `importlib` (see the top of the file). |
| Slack app object | `ctx.register_platform_handler("slack", factory)` | The factory gets `(app, adapter)`. It runs again for each new `AsyncApp`, because `connect()` rebuilds the app on reconnect (`adapter.py` around lines 1805-1811). |
| Button clicks | `ctx.register_slack_action_handler(action_id, cb)` (`hermes_cli/plugins.py:820`) | `cb(ack, body, action)`. Ack within 3 s. No client is passed in. Core only has specific action IDs, so plugin IDs don't collide. |
| Calling other tools | `ctx.dispatch_tool(name, args)` (`hermes_cli/plugins.py` around lines 686-695) | Returns a JSON string and fires no tool hooks. Use it for `radius_balance`, `radius_wallet_address`, `x402_wallet_status` and `feedback_ledger`. |
| x402 payment ledger | `$X402_LEDGER_PATH`, default `$HERMES_HOME/x402/ledger.jsonl` (`plugins/x402/__init__.py:63-65`) | JSONL with several rows per attempt (`event`: started, signing, completed). Dedupe by `id`, the way the `x402_ledger` tool does (around lines 450-470). Fields: `id, ts, url, method, memo, requested_by, outcome, amount, asset_symbol, pay_to, tx_hash, explorer_url, http_status, accepts`. |
| Feedback counts | `feedback_ledger` tool, or `$FEEDBACK_LEDGER_PATH` | With no session context, the tool returns counts only. That's all this page needs. |
| Per-call cap | `X402_MAX_PER_CALL` (`plugins/x402/__init__.py:60`), default 0.01 | **This is the only spending limit.** There is no daily cap. |
| Funding | `funding_hint` = `{EXPLORER}/wallet` (`x402` around line 475) | There's no funding flow, only a link. |
| User authorization | `adapter._is_interactive_user_authorized(user_id, *, channel_id="", user_name=None, team_id="")` (`adapter.py` around lines 5320-5367) | Private. It's what core buttons use, and it applies `SLACK_ALLOWED_USERS` / `SLACK_ALLOW_ALL_USERS` / gateway auth. |
| Who triggered a turn | `gateway.session_context.get_session_env("HERMES_SESSION_USER_ID")`, plus `_PLATFORM` and `_SCOPE_ID` (team) | feedback-ledger already reads these (around lines 520-540). |
| Post-payment hook | `post_tool_call` hook. Kwargs: `tool_name, args, result, task_id, session_id, tool_call_id, turn_id, duration_ms, status, ...` | No user ID in the kwargs; use the session ContextVars above. Hooks run on a worker thread with a copied context and a timeout. |
| Slack scopes and events | `slack/manifest.json` | `views.publish` needs **no scope**. `app_home_opened` is already subscribed. `home_tab_enabled` is currently `false`. |
| Plugin auto-enable | `scripts/entrypoint.sh` (around lines 538-615) | Every bundled `plugins/*/plugin.yaml` is enabled on boot. |
| Contract checks | `scripts/smoke_image.py` (around lines 46-60) | Checks the Hermes internals feedback-ledger depends on. Add yours here. |

---

## 2. Traps the research found (don't repeat them)

1. **Don't register an `app_home_opened` listener from the plugin. It will never run.** Hermes registers its own `app_home_opened` listener and then a catch-all `app.event(re.compile(".*"))` before it wires plugin handlers (`adapter.py` around lines 1620-1689). Bolt runs only the first listener that matches. This was confirmed against slack_bolt 1.30. **Use global middleware (`app.use`) instead**, like feedback-ledger:
   - Act only when `body["event"]["type"] == "app_home_opened"` and `event["tab"] == "home"`.
   - Schedule the publish as a background task.
   - **Always `await next()`**, so Hermes still sees the event. Hermes only handles `tab == "messages"`.
2. **Don't use the `alert` block.** Slack's docs say it's modal-only for now. Use a `section` with a warning emoji, a `context` line, or a `card` block (cards work on the Home tab).
3. **The `table` block is fine** on the Home tab. Limits: 100 rows, 20 cells per row and 10,000 characters per table.
4. **Matchers on `AsyncApp` must be `async def`.** A sync matcher raises `TypeError`. This only matters if you use matchers anywhere.
5. **Multi-workspace:** `app.client` is built from the first bot token only (`adapter.py` around line 1811). Choose the client per team with `adapter._team_clients.get(team_id)` (private, filled around line 1758), falling back to `app.client`. Look it up at call time, never cache it across reconnects.
6. **Threading:** `post_tool_call` runs on a worker thread, but `AsyncWebClient` belongs to the adapter's event loop. Capture the loop in the factory with `asyncio.get_running_loop()` and submit with `asyncio.run_coroutine_threadsafe`.
7. **`radius_balance` and `x402_wallet_status` start `radius-cli`, a Node process, and block.** Never call them on the bolt loop. Use `run_in_executor` and cache results with a TTL (`app_home_opened` fires on every open).
8. **Untrusted text:** the ledger's `memo` and `url` come from the model or the seller, and `requested_by` comes from the model. Escape mrkdwn (or use `plain_text`) and truncate. **Never use `requested_by` for identity or filtering.**
9. **A URL button still sends a `block_actions` event.** Register an ack-only handler for it and for the overflow menu, or Slack shows an error triangle.
10. **A modal submission needs `app.view(callback_id)`**, which is only reachable through the factory. Core registers no view listeners, so there's nothing to collide with. Keep the Fund wallet modal read-only (no submit) and you won't need one.

---

## 3. Design changes from the mockup

The research forced these changes:
- **It's a shared operator dashboard.** There's one deployment-wide wallet and one ledger. Every allowed user sees the same page. Title it something like "Agent wallet".
- **"Spending limits" becomes "Per-call cap"**, showing `X402_MAX_PER_CALL`. Drop "today" and "resets" until a daily cap exists.
- **The low-balance warning is a section or card**, not an alert block.
- **"Fund wallet"** opens a modal with the address (copyable) and the explorer link only. It has no submit button.
- **Payments table:** show all payments, not per user. Leave out a reliable "asked by" column, since `requested_by` can't be trusted.

---

## 4. Build plan

### Phase 1: works with today's data

New plugin `plugins/slack-home/`:
- `plugin.yaml`: name, version, description. It may provide no tools; check that the entrypoint's plugin discovery handles a plugin with no tools, and add an inert toolset if it needs one.
- `__init__.py`, structured as:

```
register(ctx):
    if not enabled(): log and return              # SLACK_HOME_TAB, default off
    ctx.register_platform_handler("slack", wire_slack)
    ctx.register_slack_action_handler("radius_home_refresh", on_refresh)
    ctx.register_slack_action_handler("radius_home_open_explorer", ack_only)
    ctx.register_slack_action_handler("radius_home_wallet_menu", on_wallet_menu)    # overflow: copy address / explorer
    ctx.register_slack_action_handler("radius_home_fund", on_fund)                  # views.open, read-only modal
    ctx.register_hook("post_tool_call", on_post_tool_call)  # x402_request / x402_benchmark only

wire_slack(app, adapter):          # idempotent per app (flag on app), like feedback-ledger
    store adapter + running loop
    app.use(home_middleware)

home_middleware(body, next):
    try: if app_home_opened and tab == "home": schedule publish(user, team)
    finally: await next()

publish(user_id, team_id):
    client = adapter._team_clients.get(team_id) or app.client
    if not authorized(user_id, team_id): views.publish(minimal_view)   # description + "ask an admin for access"
    else: views.publish(build_view(snapshot()))
    remember (user_id, team_id) as a recent viewer (bounded, e.g. LRU 200, 24h)

snapshot():   cached with a TTL (SLACK_HOME_CACHE_SECONDS, default 60); radius-cli calls in an executor
    wallet_address  <- dispatch_tool("radius_wallet_address")
    balances        <- dispatch_tool("radius_balance"), dispatch_tool("x402_wallet_status")
    payments        <- read the x402 ledger directly, dedupe by id, newest 8, mainnet first if present
    per_call_cap    <- X402_MAX_PER_CALL
    feedback        <- dispatch_tool("feedback_ledger", {"since": "7d"}) counts only
    each source fails on its own -> its section shows "unavailable", never the whole page

on_refresh(ack, body, action):  await ack(); drop the cache; publish(body.user.id, body.user.team_id)
on_post_tool_call(tool_name, ...):  if tool_name in {x402_request, x402_benchmark}:
    uid = get_session_env("HERMES_SESSION_USER_ID"); team = get_session_env("HERMES_SESSION_SCOPE_ID")
    drop the cache; run_coroutine_threadsafe(publish(uid, team), loop)   # skip if platform != slack
    (fallback when the context vars are empty: republish for recent viewers)
```

**`build_view(snapshot)`**, top to bottom. Stay under 100 blocks (about 30 expected) and 250 KB.
1. `header`: the headline value. In Phase 1, before Phase 2 exists, it can only show SBC as the headline. Don't show a fake USD total.
2. `context`: networks · "updated <time>".
3. `section` with the address in a code span, plus an `overflow` accessory (`radius_home_wallet_menu`).
4. `section` with `fields`: balances, at most 10 fields.
5. Low-balance `section` or `card`, only when a balance is below its floor.
6. `actions`: Refresh (primary, `radius_home_refresh`), Fund wallet (`radius_home_fund`), Open explorer (a URL button, `radius_home_open_explorer`).
7. `divider`, then a `section` with fields for the per-call cap and approved hosts.
8. `divider`, a `header` "Recent x402 payments", then a `table` (time, endpoint, paid, status) built from `outcome`. Under it, a `context` line: "From the local attempt ledger, not chain-reconciled".
9. `divider`, then a `section` with fields for feedback in the last 7 days (helpful / not helpful).
10. A testnet `context` line, **last on the page** and never counted in the total ("Testnet · not real money").
11. A `context` footer: template `VERSION` · "Stablecoins valued at $1.00".

Keep the plain-text strings and limits in mind: header text is 150 characters max, section text 3,000, and fields 2,000 each.

### Phase 2: mainnet balances (only after step 0 is answered)

Add read-only balance lookups: `eth_getBalance` and ERC-20 `balanceOf` over JSON-RPC.
- Assets: USDC and ETH on Base, SBC on Radius mainnet.
- Settings for RPC URLs and token addresses. Use `NEW_VARIABLE`-style names, documented in `.env.example` and the README.
- The headline is the sum in USD. Stablecoins are valued at $1.00. ETH goes through the chosen price source; if no price is available, show ETH in ETH and leave it out of the total, with a note.
- Low-balance floors as settings, e.g. `SLACK_HOME_ETH_FLOOR`.
- Outbound requests go through the egress proxy if it's enabled (see `docker-compose.egress.yml` and `scripts/egress/`). Make sure the RPC hosts are allowed.

### Manifest and docs

- `slack/manifest.json`: set `features.app_home.home_tab_enabled: true`. Consider setting `agent_tasks_enabled` explicitly; Slack enables a Tasks tab by default for `agent_view` apps (Slack changelog, 18 November 2026).
- **Unverified:** whether turning on the Home tab needs the Slack app reinstalled. The README currently says no reinstall is needed for the Slack features (around line 347), so give the Home tab its own note and test it.
- README: a "Slack Home tab" section covering what it shows, `SLACK_HOME_TAB`, the cache setting, the floors, who can see wallet data, and that the data is shared across users.
- `.env.example`: the new variables, commented out.
- Behaviour lives in the plugin. No `HERMES.md` change is needed unless the agent should mention the tab.

---

## 5. Tests and checks

`tests/test_slack_home.py`, offline, in the same style as `tests/test_feedback_ledger.py`:
- Middleware: tab=home schedules a publish. tab=messages and other events don't. `next()` is **always** awaited, including when the handler raises.
- `build_view`: block count ≤ 100. JSON ≤ 250 KB. The table stays within its limits. A missing source renders as "unavailable". Testnet comes last and isn't in the total. A malicious `memo` or `url` (mrkdwn, `<!channel>`, links) comes out escaped.
- Authorization: an unauthorized user gets the minimal view with no address or balances. Actions re-check authorization.
- Client selection: `_team_clients` is preferred over `app.client`.
- Cache: a second open within the TTL doesn't call tools again, and Refresh drops the cache.
- `post_tool_call`: only `x402_*` payment tools trigger a republish. The user comes from the session env, not from `args`.
- Ledger parsing: multi-row attempts are deduped by `id`, with the newest first.
- Plugin import and registration with a fake `ctx`.

`scripts/smoke_image.py`: add a contract check, like `FEEDBACK_CONTRACT`, that the pinned Hermes still has:
- `PluginContext.register_platform_handler`, `register_slack_action_handler` and `dispatch_tool`
- `SlackAdapter._is_interactive_user_authorized`
- `_team_clients` set on the adapter
- `gateway.session_context.get_session_env`

Before finishing, run everything in `AGENTS.md` ("Before finishing"). If you touch `scripts/smoke_image.py`, say so in the PR; CI's `container` job covers it.

**Manual test, needs a real Slack workspace:**
- Open the Home tab on desktop and mobile. Note where it appears with the agent layout; that's still unverified.
- Click Refresh.
- Make an x402 payment and check the tab updates.
- Check that a user who isn't allowed sees the minimal view.

---

## 6. Done when

- With `SLACK_HOME_TAB` unset, nothing changes.
- With it on (and `home_tab_enabled: true`), allowed users see the dashboard within a few seconds of opening the tab, and everyone else sees the minimal view.
- The Messages tab and agent DM behave exactly as before.
- Refresh and post-payment republish work, no radius-cli call ever runs on the event loop, and one failing source never blanks the page.
- All `AGENTS.md` checks pass, and the new tests and smoke contract are in.

## Out of scope

- Anything that sends money from the Home tab.
- Per-user wallets.
- Daily spending caps.
- The reaction-trigger and interactive-card features researched earlier.
