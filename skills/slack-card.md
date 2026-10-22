---
name: slack-card
description: Show or make a card in Slack (wallet card, status card, themed or fancy wallet summary) as markdown that Hermes renders as Block Kit, filled with real values from the wallet tools. Not for explaining Block Kit to developers.
---

# Slack card

Use this skill when someone asks for a "card", a "themed" or "fancy" summary, a dashboard-style reply, or a "Block Kit" message in Slack, for example "show my wallet as a card" or "make a neon wallet card".

## How Slack formatting works here

You write markdown and Hermes converts it. When your Slack platform note says Block Kit replies are on, `#`/`##` headers become header blocks, `-` lists become real lists, a pipe table becomes a native table, `---` becomes a divider, `>` becomes a quote, and a fenced block becomes a code block. When it doesn't, use the same card but replace the table with one bold label line per row.

You cannot send Block Kit JSON yourself. No tool takes it, so a `blocks` or `attachments` payload in your reply shows up as a code block. Colored sidebars and custom buttons aren't available. The only buttons are the 👍/👎 feedback buttons Hermes adds by itself. If someone asks for buttons or a color, build the card anyway and say in one short line that those aren't available.

If the user explicitly wants Block Kit JSON to paste into their own Slack app or the Block Kit Builder, give it in a fenced code block and say it's for copying. A request for a card is not a request for JSON.

## Wallet card

1. Call `radius_wallet_address` and `radius_balance` once each. `radius_balance` returns `address`, `sbc`, `rusd`, `totalUsd` and `sbcError`.
2. Use only values the tools returned. If a tool fails, write "unavailable" in that row and add one line saying what failed. Never fill in an example number.
3. Round to at most 4 decimals, shorten the address to its first 6 and last 4 characters (like 0x4Fac…1254) and link it to the explorer (`https://testnet.radiustech.xyz/address/<full address>` on Radius Testnet).
4. A theme is a word and a Unicode emoji in the header plus at most one short tagline. Use Unicode emoji such as 💼, not `:briefcase:` shortcodes, which stay as text inside tables and lists.

The card is the markdown between the two marker lines below. Replace every `<…>` placeholder with tool output and write the card straight into your reply: no code fence around it and no marker lines.

Card starts:

## 💼 <Theme> wallet · Radius Testnet

**Address** [0xABCD…1234](https://testnet.radiustech.xyz/address/<full address>)

| Asset | Balance |
|---|---:|
| SBC | <sbc> |
| RUSD (gas) | <rusd> |
| Total | ≈$<totalUsd> |

---

> <status emoji> <one line: funded / low balance / SBC read failed> · checked just now

Card ends.

For a low balance, use 💸 and suggest the `dripping-faucet` skill. For a status card about something else (an x402 payment, a deployment, an A2A call), keep the same shape: one header, a few bold label lines or one small table, a divider, and a one-line quote with the outcome.

## Limits

- One table per card, at most 5 columns and about 12 rows.
- Keep the whole reply under about 20 blocks. Each header, paragraph, list, table, divider and quote is one block. Past Slack's 50-block limit the whole reply falls back to plain text.
- Use `-` or `1.` for list items. Lines starting with `•` are not turned into a list.
- Headers are plain text: bold and links inside a header are dropped.
- Don't post to Slack through the terminal or the Slack Web API to get real Block Kit. Your reply is the message.
