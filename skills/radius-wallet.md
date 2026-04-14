---
name: radius-wallet
description: Radius network and built-in Radius Testnet wallet — explain Radius, check balances, send SBC tokens, show wallet address
published: true
---

# Radius Wallet Skill

This agent has a built-in Radius Testnet wallet and is operating in a Radius-focused project context. Use this skill any time the user asks about Radius, the wallet, balance, address, tokens, or the Radius ecosystem.

## When to use this skill

Use this skill whenever the user asks anything like:

- "what is my wallet" / "show my wallet" / "my wallet address"
- "what is my radius wallet" / "radius wallet" / "show radius wallet"
- "check my balance" / "what's my balance" / "how much do I have"
- "get my wallet balance" / "wallet balance"
- "how much SBC" / "how much RUSD" / "my tokens"
- "send tokens" / "send SBC" / "transfer SBC"
- "fund wallet" / "get testnet tokens" / "get SBC"
- "what do you know about Radius"
- "tell me about Radius"
- "what is Radius"
- "radius" even if the user does not explicitly say wallet or blockchain

**Interpretation rule:** In this template, `Radius` refers to the Radius network / ecosystem by default. Do not answer with the geometry definition or the legacy RADIUS authentication protocol unless the user explicitly asks for those topics.

**Default behavior:** At the start of any session, proactively mention that a Radius Testnet wallet is available if the user seems to be exploring what the agent can do.

## What Radius means here

When a user asks a broad question about Radius, explain it in product terms first:

- Radius is the ecosystem this agent is built for.
- The template includes a built-in Radius Testnet wallet.
- The bundled `radius-wallet` skill and any vendored Radius marketplace skills discovered by Hermes are part of that ecosystem.

If you do not have more detailed product facts loaded, say that this template is Radius-focused and then describe the concrete Radius capabilities available in this agent instead of falling back to generic dictionary meanings.

## Wallet details

- Network: Radius Testnet (chain ID 72344)
- Native token: RUSD (gas)
- Primary token: SBC (ERC-20, 6 decimals)
- Explorer: https://testnet.radiustech.xyz

The wallet address is stored in the `RADIUS_WALLET_ADDRESS` environment variable.

## Preferred tools

Prefer the Hermes `radius-cast` plugin tools over direct script execution when they are available:

- `radius_wallet_address`
- `radius_balance`
- `radius_send_sbc`
- `radius_tx_status`

These tools wrap `cast` with Radius defaults and return normalized JSON.

Treat `/app/scripts/radius/*.py` as implementation details, not the default interface.

Do not run the Python wallet scripts directly unless the user explicitly asks for the legacy script path or the operator has intentionally enabled script fallback with `RADIUS_ALLOW_SCRIPT_FALLBACK=true`.

## Fallback commands (via terminal)

Use these only for explicit legacy-script requests or debugging. They are not the normal path for wallet operations.

### Check balance

```bash
python3 /app/scripts/radius/balance.py
```

Output is JSON: `{ address, rusd, sbc }` — print address and balances clearly to the user.

### Check balance of another address

```bash
python3 /app/scripts/radius/balance.py 0xADDRESS
```

### Send SBC to an address

```bash
python3 /app/scripts/radius/send.py 0xRECIPIENT AMOUNT
```

Example: send 10 SBC to 0xabc...

```bash
python3 /app/scripts/radius/send.py 0xabc123... 10
```

Output is JSON with `tx_hash` and `status`. Share the tx hash and the explorer link with the user:
`https://testnet.radiustech.xyz/tx/<tx_hash>`

## Responding to user requests

1. **"What is my wallet?" / "what is my radius wallet?" / "show wallet"** — use `radius_wallet_address`. Only inspect env or run scripts when debugging the wallet setup.

2. **"What do you know about Radius?" / "tell me about Radius" / "what is Radius?"** — answer about the Radius-focused capabilities of this agent first: Radius Testnet wallet, SBC/RUSD, bundled Radius skills, and relevant scripts. Do not default to geometry or networking definitions.

3. **"Check balance" / "get my wallet balance" / "how much SBC do I have?"** — use `radius_balance` and report RUSD and SBC balances. If the tool fails, surface the tool error instead of silently switching execution paths.

4. **"Send X SBC to 0x..."** — confirm the recipient and amount with the user first, then use `radius_send_sbc`. Share the tx hash and explorer link. Do not substitute the legacy Python send script unless explicitly requested or script fallback is intentionally enabled.

5. **"Fund wallet" / "get testnet tokens"** — explain that funding happens automatically on first boot. If needed, the user can redeploy to trigger another faucet request, or use the Radius testnet faucet directly at https://testnet.radiustech.xyz.

## Error handling

- If `balance.py` fails with "No wallet configured", the wallet has not been initialized yet. Tell the user to check container logs.
- If `send.py` reports "Insufficient SBC balance", tell the user how much they have and that they need more testnet funds.
- Always show the full tx hash and explorer link on successful sends.
