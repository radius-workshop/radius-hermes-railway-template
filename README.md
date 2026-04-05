# Hermes Agent Railway Template

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-railway-template?referralCode=uTN7AS&utm_medium=integration&utm_source=template&utm_campaign=generic)

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) to Railway as a worker service with persistent state.

This template is worker-only: setup and configuration are done through Railway Variables, then the container bootstraps Hermes automatically on first run.

## What you get

- Hermes gateway running as a Railway worker
- First-boot bootstrap from environment variables
- Persistent Hermes state on a Railway volume at `/data`
- Telegram, Discord, or Slack support (at least one required)
- Built-in Radius Testnet **multi-wallet** support (local and/or Para, auto-funded via faucet)
- Agent discovery layer served at `/.well-known/*` — ERC 8004 registration and Cloudflare agent skills discovery

## How it works

1. You configure required variables in Railway.
2. On first boot, entrypoint initializes Hermes under `/data/.hermes`.
3. On future boots, the same persisted state is reused.
4. Container starts a Bun HTTP server (skills discovery) and `hermes gateway` in parallel.

## Railway deploy instructions

In Railway Template Composer:

1. Add a volume mounted at `/data`.
2. Deploy as a worker service.
3. Set only the variables you actually need (see below).

Template defaults (already included in `railway.toml`):

- `HERMES_HOME=/data/.hermes`
- `HOME=/data`
- `MESSAGING_CWD=/data/workspace`
- `LLM_MODEL=anthropic/claude-3.5-haiku`

## Important: how to set variables in Railway

**Only add variables you intend to use. Do not add optional variables with empty values.**

Railway injects every variable you define into the container environment, even if the value is empty. Hermes parses several variables as integers (e.g. `HERMES_MAX_ITERATIONS`, `TERMINAL_TIMEOUT`). If these are present as empty strings, Hermes will fail with a `ValueError` when processing messages.

**Right way:** add only the variables you need, with real values.

**Wrong way:** copy the full `.env.example` into Railway with all optional fields left blank.

If you want to use `.env.example` as a reference, only add the variables you plan to fill in. Leave everything else out of Railway entirely.

## Required runtime variables

Set at least one inference provider:

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `OPENAI_API_KEY` + `OPENAI_BASE_URL` | OpenAI-compatible provider |
| `ANTHROPIC_API_KEY` | Anthropic direct API |

Set at least one messaging platform:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` | Slack (both required) |

## Recommended variables

### Allowlists (strongly recommended)

Restrict access to specific user IDs. Format: plain comma-separated integers, no quotes, no brackets.

```
TELEGRAM_ALLOWED_USERS=123456789,987654321
DISCORD_ALLOWED_USERS=123456789012345678,234567890123456789
SLACK_ALLOWED_USERS=U01234ABCDE,U09876WXYZ
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### Provider selection

If you set multiple provider keys, pin which one Hermes uses:

```
HERMES_INFERENCE_PROVIDER=openrouter
```

Without this, Hermes auto-selects and may not pick the one you expect.

### Model override

```
LLM_MODEL=anthropic/claude-3.5-haiku
```

Use any model ID supported by your provider. OpenRouter model IDs look like `anthropic/claude-3.5-haiku` or `openai/gpt-4o`.

## Radius wallets

This template supports a Radius wallet registry. You can configure one or both wallet providers:

- `local` (private-key wallet managed on disk)
- `para` (Para server-side embedded wallet)

On boot, `scripts/radius/wallet-init.mjs` initializes every wallet listed in `RADIUS_WALLETS`, stores provider metadata under `/data/.hermes/.radius/wallets/<name>/`, optionally auto-funds selected wallets, and writes a manifest to:

- `/data/.hermes/.radius/wallets/manifest.json`

Backwards compatibility is preserved for local-only users (`RADIUS_PRIVATE_KEY` still works and legacy `/data/.hermes/.radius/key` + `address` are maintained).

### Radius variables

| Variable | Description |
|---|---|
| `RADIUS_WALLETS` | Comma-separated configured wallets. Example: `local,para`. |
| `RADIUS_DEFAULT_WALLET` | Default wallet used when no wallet is explicitly provided. |
| `RADIUS_AUTO_FUND_ON_BOOT` | `true/false` for faucet funding at boot. |
| `RADIUS_AUTO_FUND_WALLETS` | Which wallets to auto-fund. Example: `local,para`. |
| `RADIUS_LOCAL_PRIVATE_KEY` | Optional BYO local private key. |
| `RADIUS_LOCAL_AUTO_GENERATE` | Auto-generate local key if missing. Default: `true`. |
| `PARA_API_KEY` | Para Server SDK API key (required for para wallet). |
| `PARA_ENVIRONMENT` | Para environment (`beta` by default). |
| `PARA_WALLET_IDENTIFIER` | Stable identifier for Para wallet reuse. |
| `PARA_WALLET_IDENTIFIER_TYPE` | Default: `CUSTOM_ID`. |
| `PARA_AUTO_CREATE` | Auto-create Para wallet if it does not exist. |

Legacy vars still honored: `RADIUS_PRIVATE_KEY`, `RADIUS_WALLET_ADDRESS`, `RADIUS_AUTO_FUND`.

### Wallet commands (via chat)

- *"show my wallets"*
- *"show default wallet"*
- *"switch default wallet to para"*
- *"use the para wallet"*
- *"check local wallet balance"*
- *"send 10 SBC to 0x... using para"*
- *"fund both wallets"*

The agent runs `/app/scripts/radius/cmd-wallets.mjs` (including `--set-default=...`), `cmd-balance.mjs`, `cmd-send.mjs`, and `cmd-fund.mjs`.

## Agent discovery

This template runs a lightweight Bun/Hono HTTP server alongside Hermes that serves agent discovery endpoints at `/.well-known/*`. It binds to Railway's `PORT`, so once you generate a public domain in Railway (Settings → Networking → Generate Domain), the endpoints are live automatically.

### Endpoints

| Path | Spec | Description |
|---|---|---|
| `/.well-known/agent-registration.json` | [ERC 8004](https://eips.ethereum.org/EIPS/eip-8004) | Agent identity, wallet address, supported services |
| `/.well-known/agent-skills/index.json` | [Cloudflare Agent Skills Discovery RFC](https://github.com/cloudflare/agent-skills-discovery-rfc) | Index of published skills with digests and URLs |
| `/.well-known/agent-skills/:name/SKILL.md` | Cloudflare Agent Skills Discovery RFC | Individual skill document |

**`agent-registration.json`** advertises this agent's on-chain identity per ERC 8004. It includes the wallet address derived from `RADIUS_WALLET_ADDRESS`, x402 payment support, Radius network RPC endpoints, and faucet URLs. Customize the agent name with `AGENT_NAME`.

**`agent-skills/index.json`** lets other agents and tools enumerate what this agent can do. Each entry includes the skill name, description, a URL to fetch the full skill document, and a SHA-256 content digest so consumers can detect updates.

### Publishing a skill

Skills are opt-in. A skill file is only surfaced through the discovery endpoints if its frontmatter contains `published: true`:

```markdown
---
name: my-skill
description: What this skill does
published: true
---

# My Skill
...
```

Skills without `published: true` are installed into Hermes for the agent's own use but are never served publicly.

### Variables

| Variable | Description |
|---|---|
| `AGENT_NAME` | Display name in `agent-registration.json`. Defaults to `Hermes Agent`. |
| `DEBUG_SKILLS=1` | Enables a `/debug/skills` endpoint showing the server's runtime state. Off by default. |

## Customizing the agent with instructions

### Skills (agent knowledge files)

Skills are Markdown files that tell Hermes about available capabilities. They live at `$HERMES_HOME/skills/` and are loaded automatically per session.

Any `.md` file you place in the `skills/` directory of this repo will be copied to the Hermes skills directory on every boot. To add your own instructions:

1. Create a file like `skills/my-instructions.md`.
2. Write instructions in plain Markdown — what the agent should know, how to behave, what commands to run.
3. Redeploy. The skill will be installed on next boot.

The `radius-wallet.md` skill is already included and tells the agent how to use the wallet scripts.

### System prompt

Set `HERMES_SYSTEM_PROMPT` in Railway Variables to give the agent a persistent identity and behavior context:

```
You are a helpful assistant with a built-in Radius Testnet wallet. You can check
balances, send SBC tokens, and help users interact with the Radius blockchain.
Always confirm with the user before sending tokens.
```

## Simple usage guide

After deploy:

1. Start a chat with your bot on Telegram/Discord/Slack.
2. Ensure your user ID is in the allowlist.
3. Send a message — Hermes responds via the configured model.

Helpful first checks:

- Check Railway deploy logs for `[bootstrap]` lines to confirm initialization.
- Confirm the volume is mounted at `/data` (check Railway service settings).
- Confirm your provider API key is valid.

## Running Hermes commands manually

Use [Railway SSH](https://docs.railway.com/cli/ssh) to connect to the running container and run Hermes CLI commands:

```bash
hermes status
hermes config
hermes model
hermes pairing list
```

## Runtime behavior

`scripts/entrypoint.sh` on each boot:

1. Validates provider and platform variables are present.
2. Writes non-empty env vars to `${HERMES_HOME}/.env`.
3. Clears empty integer-typed variables from the process environment (prevents `ValueError` in Hermes).
4. Creates `${HERMES_HOME}/config.yaml` if it doesn't exist.
5. Initializes Radius wallet registry (local and/or para), generates manifest, and optionally funds configured wallets.
6. Copies all `skills/*.md` files to `${HERMES_HOME}/skills/` (overwrites on each boot).
7. Copies skills with `published: true` frontmatter to `${HERMES_HOME}/well-known-skills/` in `name/SKILL.md` structure.
8. Starts the Bun skills server in background (binds `PORT`).
9. Starts `hermes gateway` in foreground.

## Troubleshooting

**`ValueError: invalid literal for int() with base 10: ""`**
An optional integer variable (e.g. `HERMES_MAX_ITERATIONS`) is set in Railway with an empty value. Remove it from Railway Variables entirely — do not leave optional variables set to empty strings.

**`401 Missing Authentication header`**
Provider/key mismatch. Set `HERMES_INFERENCE_PROVIDER` explicitly (e.g. `openrouter`) to avoid auto-selection picking the wrong provider.

**Bot connected but no replies**
Check `TELEGRAM_ALLOWED_USERS` / `DISCORD_ALLOWED_USERS` / `SLACK_ALLOWED_USERS`. Your user ID must be in the list, or set `GATEWAY_ALLOW_ALL_USERS=true` (not recommended for public bots).

**Data lost after redeploy**
Verify the Railway volume is mounted at `/data` in your service settings. Without the volume, state is lost on every deploy.

**Radius wallet not initialized**
Check deploy logs for `[radius]` lines. If Node.js errors appear, SSH in and run:
```bash
node /app/scripts/radius/wallet-init.mjs
```

**Skill not updating after edits**
Skills now overwrite on every boot. Redeploy after editing any file in `skills/`.

## Build pinning

To pin a specific Hermes version, set the build arg in Railway:

```
HERMES_GIT_REF=main
```

Replace `main` with a tag or commit SHA to lock the version.

## Local smoke test

```bash
docker build -t hermes-railway-template .

docker run --rm \
  -e OPENROUTER_API_KEY=sk-or-xxx \
  -e TELEGRAM_BOT_TOKEN=123456:ABC \
  -e TELEGRAM_ALLOWED_USERS=123456789 \
  -v "$(pwd)/.tmpdata:/data" \
  hermes-railway-template
```


## Onboarding CLI

Generate a deploy-ready env file before first Railway deploy:

```bash
node scripts/onboarding/init-agent.mjs
```

The CLI prompts for agent name, provider keys, wallet selection (`local`, `para`, or both), default wallet, and auto-fund selection, then writes `.env.local` or `.env.railway`.
