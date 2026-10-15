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

- Network: Radius Testnet by default (`RADIUS_NETWORK`, `testnet` or `mainnet`)
- Native token: RUSD (gas)
- Primary token: SBC (ERC-20, 6 decimals)
- Chain IDs, RPC URLs and contract addresses: see the vendored `radius-dev` skill; `radius-cli` resolves them from `RADIUS_NETWORK`

This template uses a single local Radius wallet backed by `radius-cli`.

- Wallet keystore lives under `${RADIUS_HOME:-/data/.hermes/.radius-cli}`.
- The same wallet powers transfers, balance checks, DID/JWT auth, homepage wallet summary, and ERC-8004 identity.
- Para provider support is out of scope in this template revision.

## Preferred tools

Prefer the Hermes `radius-cli` plugin tools over direct script execution when they are available:

- `radius_wallet_address`
- `radius_balance`
- `radius_send_sbc`
- `radius_tx_status`

These tools wrap `radius-cli` and return normalized JSON.

Usage rules:

- Single persistent local wallet from radius-cli is always used.
- `provider` overrides are not part of this template revision.

Tool parameter guidance:

- `radius_wallet_address()`
- `radius_balance({ address })`
- `radius_send_sbc({ to, amount_sbc })`

## Responding to user requests

1. **"What is my wallet?" / "what is my radius wallet?" / "show wallet"** — use `radius_wallet_address`.

2. **"What do you know about Radius?" / "tell me about Radius" / "what is Radius?"** — answer about the Radius-focused capabilities of this agent first: Radius Testnet wallet, SBC/RUSD, bundled Radius skills, and relevant scripts. Do not default to geometry or networking definitions.

3. **"Check balance" / "get my wallet balance" / "how much SBC do I have?"** — use `radius_balance` and report RUSD and SBC balances. If the tool fails, surface the tool error instead of silently switching execution paths.

4. **"Send X SBC to 0x..."** — confirm the recipient and amount with the user first, then use `radius_send_sbc`. Share the `tx_hash` and the `explorer_url` it returns.

5. **"Fund wallet" / "get testnet tokens"** — explain that the faucet is requested automatically only on the wallet's first boot (when `${RADIUS_HOME}/initialized` does not exist yet). A redeploy does **not** request funds again, because the marker persists on the volume. To top up, use the `dripping-faucet` skill, the Radius testnet web faucet at https://testnet.radiustech.xyz/wallet, or have someone send SBC to the wallet address, plus a little RUSD for gas.

## Error handling

- If a wallet tool reports that `radius-cli` is missing or the keystore is unavailable, the wallet has not been initialized yet. Tell the user to check container logs for `[radius]` and `[bootstrap]` lines.
- If `radius_send_sbc` fails for lack of funds, check `radius_balance`, tell the user how much they have, and that they need more testnet funds.
- Always show the full tx hash and explorer link on successful sends.
