---
name: slack-agent
description: What we learned building this repo's Slack features on the pinned Hermes — plugins that hook into Slack (slack-react, slack-home, feedback-ledger), App Home and Block Kit design limits, Slack app configuration, and emoji reactions including custom emoji. Use before changing anything under plugins/slack-*, plugins/feedback-ledger, slack/manifest.json, the Slack parts of HERMES.md or skills/slack-*.md, or when someone reports that the bot can't do something in Slack.
---

# Working on the Slack agent

Notes for whoever changes the Slack side of this repo next. Each point was checked against Hermes `v2026.9.24` (the `HERMES_GIT_REF` pin) or seen on the live app in November 2026. Hermes line numbers move; re-check before relying on one. When the pin changes, re-run the contract tests below.

This file is for people and coding agents working on the repo. What the deployed agent should know goes in `HERMES.md` and `skills/*.md`.

## How to extend Slack without patching Hermes

Don't fork or patch Hermes. Write a bundled plugin under `plugins/<name>/`. `plugins/feedback-ledger`, `plugins/slack-react` and `plugins/slack-home` are the patterns to copy.

- **Get the Slack app.** `ctx.register_platform_handler("slack", factory)` calls `factory(app, adapter)` with the live slack_bolt `AsyncApp`. It runs again for every new app, because a reconnect builds a new one. Mark the app once wired (`app._radius_<plugin>_wired`) so middleware isn't added twice, and keep the newest `app`, `adapter` and running loop.
- **slack_bolt runs only the first matching listener.** Hermes already listens for `app_home_opened` (it uses it for the Messages tab) and for the `hermes_feedback` action. A second listener for either never runs. Add a global middleware with `app.use(...)` instead: it sees every request first. It must filter narrowly, do no slow work, and always `await next()`.
- **Buttons.** `ctx.register_slack_action_handler(action_id, cb)` with `async def cb(ack, body, action)`. No client is passed in; use the app you kept. Ack within 3 seconds. A `trigger_id` for `views.open` also expires after 3 seconds, so build modals from cached data. A link button (`url`) still sends a click that needs an ack; register a handler for it.
- **Tools that call Slack.** Register with `is_async=True`. Hermes runs the handler on a different event loop from the gateway's, so send the Slack call to the loop you saved with `asyncio.run_coroutine_threadsafe` (see `_call` in `plugins/slack-react`).
- **Who and where.** Read the conversation from `gateway.session_context.get_session_env`: `HERMES_SESSION_PLATFORM`, `_CHAT_ID`, `_THREAD_ID`, `_MESSAGE_ID` (the message that started the turn), `_USER_ID`, `_SCOPE_ID` (team). Never take a channel from the model. A tool that acts in Slack should act only in the conversation the turn came from.
- **Hooks.** `ctx.register_hook("post_tool_call", cb)` gets `tool_name` and no user id; read the session variables above inside the hook. It runs on a worker thread, so don't block in it.
- **Authorization.** Outside a turn (Home tab opens, button clicks) use `adapter._is_interactive_user_authorized(user_id, channel_id=..., team_id=...)`. It applies `SLACK_ALLOWED_USERS`, `SLACK_ALLOW_ALL_USERS` and the gateway rules. If the check can't run, show nothing sensitive.
- **Multi-workspace.** `adapter._get_client(channel, team_id=...)` picks the workspace's client; fall back to `app.client`.
- **Private Hermes methods.** Every private name a plugin depends on needs two guards: a check in `scripts/smoke_image.py` (runs in CI's `container` job) and a `PinnedHermesContractTests` test that skips when Hermes isn't importable.
- **Plugin loading.** `scripts/entrypoint.sh` enables every `plugins/*/plugin.yaml` on boot and enables a toolset named after the plugin, so name the toolset the same as the plugin.

## Testing

- Load a plugin in tests with `importlib` (see the top of `tests/test_slack_react.py`). Plugin directories have hyphens and aren't importable by name.
- To run the contract tests, or to drive a plugin through the real adapter, fetch the pinned Hermes and put it on `PYTHONPATH`:

  ```sh
  git init hermes-src && cd hermes-src
  git remote add origin https://github.com/NousResearch/hermes-agent.git
  git fetch --depth 1 origin v2026.9.24 && git checkout FETCH_HEAD
  PYTHONPATH=$PWD python -m pytest /path/to/repo/tests/test_slack_home.py -k Pinned
  ```

  Run Python from outside the repo root when importing Hermes: the repo's own `plugins/` directory shadows Hermes' `plugins` package.
- A real `AsyncApp` can be dispatched offline: build it with a fake `authorize`, call `adapter._register_bolt_handlers()`, then `app.async_dispatch(AsyncBoltRequest(body=..., mode="socket_mode"))`. `tests/test_feedback_ledger.py` does this.
- None of this proves how Slack renders a view. Check on the live app.

## Reactions and custom emoji ("slackmojis")

- The 👀 on a message addressed to the bot, and the ✅ or ❌ that replaces it, are added by the Hermes Slack adapter, not by the model. `SLACK_REACTIONS=false` turns them off. The model can't see them, so without a note in `HERMES.md` it denies having added them.
- Hermes gives the model no way to react in Slack. `send_message` has a `react` action, but the Slack adapter has no `add_reaction` method, and `ctx.platform_actions.add_reaction` covers Telegram and Discord only. `plugins/slack-react` adds the `slack_react` tool.
- `reactions.add` takes the emoji **name** without colons. A workspace custom emoji works exactly like a standard one. A pasted emoji character is not a name.
- Useful error codes: `invalid_name` (no emoji with that name here), `already_reacted` and `no_reaction` (treat as success), `missing_scope` (needs `reactions:write`, already in the manifest).
- The bot can remove only its own reactions.
- The bot can't list the workspace's custom emoji. That needs the `emoji:read` scope, which the manifest doesn't request. Until it does, the bot must be told the exact name.
- The thread context Hermes gives the model has no message timestamps. The model knows the triggering message and the thread parent; for any other message it needs a link. The `p1760649990000200` at the end of a Slack message link is the timestamp `1760649990.000200`.
- In a reply, `:name:` in the text renders the emoji, custom ones included. That needs no tool.

## Slack app configuration

- `slack/manifest.json` describes the app, but changing the file does not change an installed app. Apply the change in the app's settings at api.slack.com/apps, or push the manifest with the Slack CLI (README, "Slack app from code").
- **Find the right app.** There can be several apps with the same name. In Slack open the app, go to **About**, and follow the settings link from there.
- **Home tab.** App settings → **App Home** → **Show Tabs** → **Home Tab**. Then reload Slack (Cmd+R). The tab appears first, before Messages, About and Tasks. `views.publish` needs no scope. Until the toggle is on, the plugin receives no Home events and logs nothing.
- The app uses `agent_view` (Slack's agent layout). It still has a Home tab. `app_home_opened` arrives for both the Home and Messages tabs; check `event.tab`.
- Scopes already in the manifest that these plugins use: `reactions:write`, `reactions:read`, `chat:write`, `assistant:write`.
- A deploy restarts the gateway and interrupts anyone talking to the bot. Ask the operator first. The service may be running a tree that is ahead of `main`; check what is live before deploying, or you roll changes back.
- After a deploy, the boot log should show, per plugin, `[Slack] Wired native handlers from plugin '<name>'`, then `[slack-react] ready to add reactions`, `[slack-home] publishing the App Home tab` and `✓ slack connected`. About 30 seconds of `JWT verification failed` on `/health` before that is the entrypoint's wait loop, not a fault.

## Designing for the Home tab

What Block Kit did on the live Home tab in November 2026, on desktop and on the iOS app:

- **No large type.** A `header` block is the biggest text there is, and it renders only slightly larger than bold body text. A design with a 40px headline number can't be matched.
- **`section` fields are two columns across the full width.** On a wide window the second column starts at the middle of the pane, far from the first. There is no three- or four-column layout, and a section holds at most 10 fields.
- **Slack auto-links anything that looks like a URL**, including a bare `host/path` in mrkdwn. Long endpoints become long blue links. Set `"verbatim": true` on the mrkdwn text object to stop it, or put the text in a table's `raw_text` cell, and shorten it either way (`short_endpoint` in `slack-home`).
- **Date tokens work.** `<!date^{unix}^{date_short_pretty} {time}|fallback>` shows in the reader's own time zone ("Yesterday 3:13 PM").
- **`table` blocks: Home tabs and messages, not modals** (Slack's docs, November 2026). `slack-home` shows recent payments as a table and keeps text lines for the "All payments" modal. If `views.publish` fails with an `invalid…` error while the view holds a table, it publishes the text-line version and stops trying the table until restart. A table holds 100 rows, 20 columns and 10,000 characters. A cell is `raw_text` (shown as typed) or `rich_text` (needed for a link, a `date` element or bold).
- **`alert` blocks are untested on the Home tab.** `slack-home` uses a section with a warning emoji.
- **Put secondary links in an `overflow` menu** on a section, not in more buttons: buttons stack full-width on mobile. An overflow holds up to five options, and an option may carry a `url`. A click on any option, link or not, still needs an ack.
- **Limits.** 100 blocks per view, 3000 characters per section text, 25 elements per actions block, 24 characters for a modal title.
- **Ledger text is untrusted.** `url`, `requested_by` and similar fields come from sellers or the model. Escape `&`, `<`, `>`, strip mrkdwn markers and truncate before putting them in a view. `requested_by` is free text and usually a display name, not a user id; render a mention only when it matches a user id.
- **Show "Unavailable", never zero,** when a balance can't be read, and leave it out of a total with a note.
- **Don't draw limits the code doesn't have.** The first design showed a daily budget and a reset time; the template has only `X402_MAX_PER_CALL`.
- **No copy button, no QR code.** Block Kit can't copy to the clipboard.
- **On mobile, text in a view can't be selected**, code blocks included. Seen on iOS: the address in a code block in a modal could not be copied. A text field can be selected, so put a value people must copy in a `plain_text_input` with `initial_value`. That has two costs. A modal with an input block must have a `submit` button, and the submission needs an ack: register `app.view(callback_id)` on the app you were handed (Hermes registers no view listeners, so your own callback id is safe). And the field is editable, so say that editing changes nothing and read nothing from it. Desktop can select a code block, which hides the problem.
- **A label promises what the control does.** The first table build had an "All payments" button that opened the latest 25. Name the limit ("Latest 25 payments"), or page through everything.
- **Shortening must keep the part that differs.** `host/…/last-segment` turned five calls to five merchants into five identical `www.x402scan.com/…/stats` rows. Keep the end of the path, cut long ids rather than dropping them, and drop `www.`. Check shortened text against real ledger rows; the test fixtures had short URLs and showed nothing wrong.
- **What a table looks like on a phone.** About two columns fit; the rest scroll sideways, so put the columns people need first. The first row is drawn as a header. A `date` element with `{date_short_pretty} {time}` reads "Nov 19 3:13 PM". Buttons in an actions block stack full-width, one per row, and section fields stack in one column. A QR code would mean sending the address to an image service.

- **Keep the phone's first screen for the total and recent payments.** Seen on iOS, November 2026: with the stablecoin and ETH sections on the tab, recent payments started below the fold. The breakdown is now a **Balances** button (a section accessory beside the "updated" line, so it doesn't add a full-width button row) that opens a modal from the cached snapshot.

Open at the time of writing:

- Whether the address in a text field can be copied on iOS and Android. It is built on that expectation and not yet checked on a phone.
- How the table looks on desktop. Only mobile screenshots have been seen.
- The "Ask the agent" button uses `slack://app?team=<T>&id=<A>&tab=messages`. It renders; nobody has confirmed that clicking it opens the Messages tab.
- How the Home tab looks on mobile in the agent layout.
- Whether recent payments now show above the fold on a phone, and how the Balances accessory button sits beside the "updated" line.

## How to run a design iteration

Both Home tab rounds went wrong in ways no test could see, and both were caught from a screenshot. So:

1. **Start from what Slack can draw.** Mock the view with Block Kit's real blocks at Slack's real sizes, for desktop and for a phone. A free-form mockup gets built as something else and the difference reads as a bug.
2. **Check each block against each surface** in Slack's reference before building: Home tab, modal and message support different blocks (tables: not in modals).
3. **Build with a fallback** for any block that is new to the app, so a refused view degrades instead of leaving the tab blank, and log when the fallback is used.
4. **Deploy, then look at it on desktop and on a phone**, with real data. Try every control: open each modal, each menu item, each link, and try to copy anything meant to be copied.
5. **Write down what you saw here**, including what is still unchecked. Move a line out of "Open" only when someone has seen it work.

Ask the operator before each deploy; it restarts the gateway.

## Keeping this skill current

Add to it when Slack surprises you: a block that renders differently than its reference suggests, a limit you hit, a setting that had to be changed in the app's settings, a Hermes method a plugin now depends on. Say how you know (docs, test, or seen live, with client and month), because the three age differently. Delete a line when the code or Slack has moved on.

## Replies in channels and threads

- The model writes markdown and Hermes converts it. It cannot send Block Kit itself; `skills/slack-card.md` covers "cards". Don't teach it to post through the Slack API from a terminal (`HERMES.md` forbids that).
- When someone reports "the bot says it can't do X in Slack", check three things in order: whether Hermes exposes a tool for X on Slack at the pinned version, whether the app has the scope, and whether `HERMES.md` tells the model the tool exists. The reaction problem was the first and the third; the scope was already there.
