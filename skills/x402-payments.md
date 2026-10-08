---
name: x402-payments
description: Pay x402 (HTTP 402) APIs from the agent wallet on Radius and Base, demo them with grug402.dev, and keep a payment paper trail
published: true
---

# x402 Payments Skill

This agent can pay for HTTP APIs that answer `402 Payment Required` with an x402 challenge, using its own wallet. The same EVM key pays on every supported network.

## When to use this skill

- "pay for / call this x402 API", "hit this paid endpoint", "buy the weather", "demo x402"
- "what x402 APIs can you call?", "show me the grug402 catalog"
- "what did you spend?", "show the payment ledger / receipts / paper trail"
- "can you pay on Base?", "what's my Base Sepolia USDC balance?"

## Tools

| Tool | Use |
|---|---|
| `x402_catalog({ network, query, include_quirks, limit })` | List demo endpoints on grug402.dev. `network`: `radius-testnet` (default), `base-sepolia`. |
| `x402_request({ url, method, body, headers, network, max_amount, memo, requested_by, dry_run })` | Call an endpoint, pay the 402 if needed, return the body and settlement tx. |
| `x402_ledger({ limit, outcome })` | Read the persistent payment ledger with paid totals. |
| `x402_wallet_status()` | Wallet address and payment-asset balances per network, plus funding hints. |
| `x402_benchmark({ networks, scenario, urls, calls, concurrency, budget, warmup, requested_by })` | Timed, concurrent payment benchmark across networks. Use for any latency, throughput, or "Radius vs Base" comparison. |

Do not shell out to `radius-cli wallet x402` or write signing code; `x402_request` already routes Radius payments through radius-cli (Permit2) and signs Base payments as EIP-3009.

## Networks

| Network | Asset | How it settles | grug402 prefix |
|---|---|---|---|
| Radius Testnet `eip155:72344` (default) | SBC | Permit2 via Radius facilitator | `/api/x402/<id>` |
| Base Sepolia `eip155:84532` | USDC | EIP-3009 via x402.org facilitator | `/api/x402-base-sepolia/<id>` |
| Radius Mainnet `eip155:723487` | USDC (real funds) | Permit2 | `/api/x402-mainnet/<id>` — refused unless `X402_ALLOW_MAINNET=true` |

The same scenario id exists on every network; only the URL prefix changes.

## How to run a demo

1. If the user has not picked an endpoint, call `x402_catalog` and offer two or three themed ones (`weather-now`, `fx-rate`, `joke-api`, `haiku-generator` (POST), `quote-api`).
2. For a first-time or unfamiliar endpoint, call `x402_request` with `dry_run: true` and show the quote (price, asset, network, payTo).
3. Pay with `x402_request`, always passing a short `memo` (why) and `requested_by` (the Slack user's name or ID when known).
4. Reply using the **Reporting a payment** template below.
5. For a side-by-side demo, call the same scenario on `radius-testnet` and `base-sepolia` and compare.

Demo prices are 0.0001 per call, well under the default cap, so you can pay testnet demo endpoints without asking first. Ask before paying anything that is not a grug402 testnet URL, or when the quote is above 0.001.

## Reporting a payment

Follow the Slack formatting rules in `HERMES.md`. A single paid call looks like this:

```
✅ Bought the weather on **Radius Testnet** for **0.0001 SBC**: 21°C, clear in Radius City.
• **Endpoint:** `weather-now` on grug402.dev
• **Settled:** [tx 0xa16e…1dec](https://testnet.radiustech.xyz/tx/<full hash>)
• **Wallet:** [0x4Fac…1254](https://testnet.radiustech.xyz/address/<full address>) · 0.4998 SBC left
🧾 Logged to the x402 ledger. Say "show ledger" for receipts.
```

• Lead with the useful result (the data that was bought), not the protocol steps.
• Use `explorer_url` from the tool result for the tx link. Use `x402_wallet_status` only when the balance matters (low balance or the user asked for it).
• For a quote (`dry_run`), use 🧾 and say nothing was charged. For `refused`, use ⛔ and name the guardrail. For `payment_failed`/`error`, use ⚠️ with the plain-language cause and fix.
• For a multi-network comparison, use one line per network: "**Base Sepolia** · 0.0001 USDC · ✅ · [tx 0x…](url)".
• Leave out payTo, asset contract, atomic amounts, CAIP ids, and the raw challenge unless the user asks for "details" or "debug".

## Benchmarks

Always use `x402_benchmark` for speed or scale comparisons. Never loop `x402_request` and read ledger timestamps: those intervals are mostly your own thinking time between tool calls.

• Default: the same grug402 scenario on Radius Testnet and Base Sepolia, so only the rail differs. Start with `calls: 10, concurrency: [1, 5]`, then go up if asked.
• Before running, state the plan in one line (networks, calls × levels, estimated spend). Testnet-only runs under the default budget need no confirmation. Ask first if any network is mainnet or the budget is above 0.05.
• For mainnet, pass `urls` per network. Say clearly when the sellers differ, because then the comparison is not apples to apples.
• If the tool refuses for budget or balance, relay the reason and the fix. Don't retry around it.

Report format:

```
⚡ **Radius Testnet vs Base Sepolia** · `weather-now` · 10 calls × c1/c5
• **Radius Testnet** · c1 p50 0.94s / p95 1.05s · c5 p50 2.1s · 1.35 paid/s · 10/10 ✅
• **Base Sepolia** · c1 p50 …s / p95 …s · c5 p50 …s · … paid/s · 10/10 ✅
• **Spend:** 0.001 SBC + 0.001 USDC · sample [tx 0x…](url)
Takeaway: <one sentence on what differs, citing paid_ms p50 at c1 and how it changes under concurrency>
```

• `paid_ms` is the headline (verify + settle + response). Mention `sign_ms` only if it is large.
• Include the caveats from the tool's `notes` only when someone draws a strong conclusion: one wallet, different facilitators, and end-to-end time rather than block time.
• Mention that the full per-call JSON was saved (`report_path`) and the payments are in the ledger.

## Guardrails

- Every attempt is written to the ledger, including refusals and failures. Never claim a payment succeeded unless `outcome` is `paid` and there is a `tx_hash`.
- `X402_MAX_PER_CALL` (default `0.01`) is a hard per-call cap; `max_amount` can only lower it.
- `outcome: refused` means a guardrail stopped it; explain which one rather than retrying around it.
- grug402 also serves 79 intentionally broken "quirk" endpoints. Only call them (`include_quirks: true`) when the user asks to see failure modes, and narrate what the quirk demonstrates.

## Paper trail

When asked for receipts, a spend summary, or "what happened", call `x402_ledger`. Show the newest 5 entries (more only if asked), one line each: `time · who · endpoint · amount · network · status emoji · [tx 0x…](url)`. End with one **Totals** line built from `paid_totals`. No tables.

## Funding

- Radius Testnet SBC: the wallet is dripped on first boot; top up via the `dripping-faucet` skill or https://testnet.radiustech.xyz/wallet.
- Base Sepolia USDC: https://faucet.circle.com (choose Base Sepolia). The payer needs no ETH because EIP-3009 settlement gas is paid by the facilitator.

If a Base payment fails verification and `x402_wallet_status` shows 0 USDC on Base Sepolia, the cause is the empty balance; give the user the wallet address and the Circle faucet link.
