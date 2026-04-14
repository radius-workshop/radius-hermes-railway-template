# Radius Wallet Skill

This agent supports **multiple Radius wallet providers** and can use either local or Para wallets per action.

Wallet tools are now registered via the `radius-wallet` plugin (toolset `radius_wallet`). The shell commands below are the underlying implementations used by that plugin.

## Wallet model

- Wallet manifest: `RADIUS_WALLET_MANIFEST` (JSON file)
- Configured wallets come from `RADIUS_WALLETS` (e.g. `local,para`)
- Default wallet comes from `RADIUS_DEFAULT_WALLET`

Use this command to inspect wallets:

```bash
node /app/scripts/radius/cmd-wallets.mjs
```

## Available commands (via terminal)

### List wallets / default wallet

```bash
node /app/scripts/radius/cmd-wallets.mjs
```

### Switch default wallet

```bash
node /app/scripts/radius/cmd-wallets.mjs --set-default=local
node /app/scripts/radius/cmd-wallets.mjs --set-default=para
```

### Check balance (default wallet)

```bash
node /app/scripts/radius/cmd-balance.mjs
```

### Check balance (explicit wallet)

```bash
node /app/scripts/radius/cmd-balance.mjs --wallet=local
node /app/scripts/radius/cmd-balance.mjs --wallet=para
```

### Send SBC (default wallet)

```bash
node /app/scripts/radius/cmd-send.mjs 0xRECIPIENT AMOUNT
node /app/scripts/radius/cmd-send.mjs --asset=rusd 0xRECIPIENT 5
```

### Send SBC (explicit wallet)

```bash
node /app/scripts/radius/cmd-send.mjs --wallet=para 0xRECIPIENT 10
```

### Fund from faucet

```bash
node /app/scripts/radius/cmd-fund.mjs --wallet=local
node /app/scripts/radius/cmd-fund.mjs --wallet=para
node /app/scripts/radius/cmd-fund.mjs --wallet=all
node /app/scripts/radius/cmd-fund.mjs --wallet=both   # backward compatible alias
```

## Responding to user requests

- “show my wallets” / “list wallets” → run `cmd-wallets.mjs`.
- “show default wallet” → use `cmd-wallets.mjs` and report `defaultWallet`.
- “use the para wallet” or “use local wallet” → run the requested command with `--wallet=...`.
- “fund my para wallet” / “fund all wallets” → run `cmd-fund.mjs` with explicit wallet target.
- “send 10 SBC to 0x... using para” → confirm recipient + amount, then run `cmd-send.mjs --wallet=para ...`.
- “check local wallet balance” → run `cmd-balance.mjs --wallet=local`.

Always return tx hash + explorer link for sends and funding transactions.

If Para setup is incomplete, run this Para docs bootstrap prompt in your coding agent:
`Fetch https://docs.getpara.com/skill.md and help me build with Para`.
