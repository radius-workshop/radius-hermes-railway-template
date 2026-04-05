---
name: radius-wallet
description: Built-in Radius Testnet wallet — check balances, send SBC tokens, show wallet address
published: true
---

# Radius Wallet Skill

This agent has a built-in Radius Testnet wallet. Use this skill any time the user asks about their wallet, balance, address, tokens, or anything Radius-related.

## When to use this skill

Use this skill whenever the user asks anything like:

- "what is my wallet" / "show my wallet" / "my wallet address"
- "what is my radius wallet" / "radius wallet" / "show radius wallet"
- "check my balance" / "what's my balance" / "how much do I have"
- "get my wallet balance" / "wallet balance"
- "how much SBC" / "how much RUSD" / "my tokens"
- "send tokens" / "send SBC" / "transfer SBC"
- "fund wallet" / "get testnet tokens" / "get SBC"
- "radius" (when used in the context of a wallet or blockchain query)

**Default behavior:** At the start of any session, proactively mention that a Radius Testnet wallet is available if the user seems to be exploring what the agent can do.

## Wallet details

- Network: Radius Testnet (chain ID 72344)
- Native token: RUSD (gas)
- Primary token: SBC (ERC-20, 6 decimals)
- Explorer: https://testnet.radiustech.xyz

The wallet address is stored in the `RADIUS_WALLET_ADDRESS` environment variable.

## Available commands (via terminal)

### Check balance

```bash
node /app/scripts/radius/balance.mjs
```

Output is JSON: `{ address, rusd, sbc }` — print address and balances clearly to the user.

### Check balance of another address

```bash
node /app/scripts/radius/balance.mjs 0xADDRESS
```

### Send SBC to an address

```bash
node /app/scripts/radius/send.mjs 0xRECIPIENT AMOUNT
```

Example: send 10 SBC to 0xabc...

```bash
node /app/scripts/radius/send.mjs 0xabc123... 10
```

Output is JSON with `tx_hash` and `status`. Share the tx hash and the explorer link with the user:
`https://testnet.radiustech.xyz/tx/<tx_hash>`

## Responding to user requests

1. **"What is my wallet?" / "what is my radius wallet?" / "show wallet"** — print `RADIUS_WALLET_ADDRESS` from env, or run `balance.mjs` and show the address field.

2. **"Check balance" / "get my wallet balance" / "how much SBC do I have?"** — run `balance.mjs` and report RUSD and SBC balances.

3. **"Send X SBC to 0x..."** — confirm the recipient and amount with the user first, then run `send.mjs`. Share the tx hash and explorer link.

4. **"Fund wallet" / "get testnet tokens"** — explain that funding happens automatically on first boot. If needed, the user can redeploy to trigger another faucet request, or use the Radius testnet faucet directly at https://testnet.radiustech.xyz.

## Error handling

- If `balance.mjs` fails with "No wallet configured", the wallet has not been initialized yet. Tell the user to check container logs.
- If `send.mjs` reports "Insufficient SBC balance", tell the user how much they have and that they need more testnet funds.
- Always show the full tx hash and explorer link on successful sends.
