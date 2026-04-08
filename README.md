# Hermes Agent Railway Template

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-railway-template?referralCode=uTN7AS&utm_medium=integration&utm_source=template&utm_campaign=generic)

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) to Railway as a worker service with persistent state.

This template is worker-only: setup and configuration are done through Railway Variables, then the container bootstraps Hermes automatically on first run.

## What you get

- Hermes gateway running as a Railway worker
- First-boot bootstrap from environment variables
- Persistent Hermes state on a Railway volume at `/data`
- Telegram, Discord, or Slack support (at least one required)
- Built-in Radius Testnet wallet (auto-generated on first boot, auto-funded via faucet)
- Agent discovery layer served at `/.well-known/*` — ERC 8004 registration, Cloudflare agent skills discovery, and A2A agent card
- Agent-to-agent (A2A) communication with two execution modes: direct (inline `message/send` + `message/stream`) and delegated (webhook-backed async submission)
- Persistent cryptographic identity derived from the wallet key — the same `RADIUS_PRIVATE_KEY` signs both transactions and JWTs

## How it works

1. You configure required variables in Railway.
2. On first boot, entrypoint initializes Hermes under `/data/.hermes`.
3. On future boots, the same persisted state is reused.
4. Container starts the Python/FastAPI agent server and `hermes gateway` in parallel.

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

## Discord bot setup

If you're using Discord, you need to create a bot application and enable the correct intents before the token will work.

### 1. Create the bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**.
2. Give it a name, then open the **Bot** tab on the left sidebar.
3. Click **Reset Token** to generate your bot token — copy it now (you won't see it again without resetting). This goes in `DISCORD_BOT_TOKEN`.

### 2. Enable privileged intents

Still on the **Bot** tab, scroll down to **Privileged Gateway Intents** and enable:

- **Server Members Intent** — required for the bot to see guild members
- **Message Content Intent** — required for the bot to read message content (without this, Hermes receives empty messages)

Click **Save Changes**. Skipping this step is the most common reason Discord bots connect but never respond.

### 3. Invite the bot to your server

1. Go to the **OAuth2 → URL Generator** tab.
2. Under **Scopes**, check `bot`.
3. Under **Bot Permissions**, check at minimum: `Send Messages`, `Read Message History`, `View Channels`.
4. Copy the generated URL at the bottom and open it in your browser.
5. Select your server and click **Authorize**.

### 4. Get your Discord user ID

To populate `DISCORD_ALLOWED_USERS`, you need your Discord user ID (a large integer, not your username):

1. In Discord, go to **Settings → Advanced** and enable **Developer Mode**.
2. Right-click your name anywhere in Discord and select **Copy User ID**.

Use that value in Railway:
```
DISCORD_ALLOWED_USERS=123456789012345678
```

---

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

## Radius wallet

This template includes a built-in Radius Testnet wallet. On first boot, the entrypoint:

1. Generates a private key (or uses `RADIUS_PRIVATE_KEY` if you set one).
2. Persists the key and address under `/data/.hermes/.radius/`.
3. Requests SBC testnet tokens from the Radius faucet.
4. Installs a wallet skill so Hermes knows how to use it.

The agent can then check balances, send SBC tokens, and show explorer links — all via natural language in chat.

### Radius variables (all optional)

| Variable | Description |
|---|---|
| `RADIUS_PRIVATE_KEY` | BYO private key (`0x...`). Auto-generated if not set. |
| `RADIUS_WALLET_ADDRESS` | Derived from key automatically. |
| `RADIUS_AUTO_FUND` | Set to `false` to skip faucet on boot. Default: enabled. |

The wallet key is stored at `/data/.hermes/.radius/key` with permissions `600`. It persists across redeploys via the Railway volume.

### Wallet commands (via chat)

Once deployed, you can ask the agent:

- *"What is my wallet address?"*
- *"Check my balance"*
- *"Send 10 SBC to 0x..."*
- *"Get testnet tokens"*

The agent runs the preconfigured Node.js scripts at `/app/scripts/radius/` using its terminal tool.

## Linear integration

This template includes a built-in Linear skill. The agent can create and update issues, query projects, add comments, and manage team workflows via the [radius-workshop/linear-claude-skill](https://github.com/radius-workshop/linear-claude-skill) tooling, which is compiled and bundled into the container image at `/app/scripts/linear-skill`.

### Prerequisites

1. Go to [linear.app](https://linear.app) → **Settings** → **Security & access** → **Personal API keys**
2. Click **Create key** — copy the key (starts with `lin_api_`)
3. Add it to Railway as `LINEAR_API_KEY`

### Linear variables

| Variable | Required | Description |
|---|---|---|
| `LINEAR_API_KEY` | Yes (for Linear) | Personal API key from Linear Settings → API |
| `LINEAR_TEAM_ID` | No | Scopes operations to a specific team. Find it via the agent: "list my Linear teams" |
| `LINEAR_PROJECT_ID` | No | Default project for issue operations. Find it via: "show my Linear projects" |

### What the agent can do

Once `LINEAR_API_KEY` is set, you can ask the agent:

- *"List open issues in the [project] project"*
- *"Create a bug: users can't log in on mobile"*
- *"Mark ENG-123 as done"*
- *"What's the status of the [project] project?"*
- *"Add a comment to ENG-456: found the root cause"*
- *"Create a sub-issue under ENG-100 for the API work"*
- *"Show me all issues assigned to me"*

The agent follows Linear best practices: each new issue gets a detailed description, labels (type + domain), and a project assignment.

### Scoping to a specific project

Set `LINEAR_PROJECT_ID` to focus the agent on a specific project by default. The agent will use it as the default context when you ask about issues or ask it to create something without specifying a project.

To find your project ID, ask the agent: *"List my Linear projects and show their IDs"*

### Delegating from Linear (coming soon)

Inbound delegation — assigning Linear issues to the agent or @mentioning it in comments — requires a webhook integration and is planned for a future release.

---

## Agent discovery

This template runs a lightweight Python/FastAPI HTTP server alongside Hermes that serves agent discovery endpoints at `/.well-known/*`. It binds to Railway's `PORT`, so once you generate a public domain in Railway (Settings → Networking → Generate Domain), the endpoints are live automatically.

### Endpoints

| Path | Auth | Spec | Description |
|---|---|---|---|
| `/.well-known/did.json` | Public | [W3C DID](https://www.w3.org/TR/did-core/) | DID document for this agent's `did:web` identity — public key, verification methods |
| `/.well-known/agent-card.json` | Public | [A2A](https://github.com/a2aproject/A2A) | A2A agent card — identity, skills, supported interfaces, auth scheme |
| `/.well-known/agent-registration.json` | Public | [ERC 8004](https://eips.ethereum.org/EIPS/eip-8004) | On-chain identity, wallet address, supported services |
| `/.well-known/agent-skills/index.json` | Public | [Cloudflare Agent Skills Discovery RFC](https://github.com/cloudflare/agent-skills-discovery-rfc) | Index of published skills with digests and URLs |
| `/.well-known/agent-skills/:name/SKILL.md` | Public | Cloudflare Agent Skills Discovery RFC | Individual skill document |

**`agent-card.json`** is the A2A discovery document. Other agents fetch it to learn how to authenticate and what this agent can do. It includes the agent's `did:web` identity (derived from `RADIUS_PRIVATE_KEY`), the `POST /a2a` interface, and the `bearer_jwt` security scheme. Skills are pulled live from the skill discovery index.

**`agent-registration.json`** advertises this agent's on-chain identity per ERC 8004. It includes the wallet address derived from `RADIUS_WALLET_ADDRESS`, the agent's `did:web`, x402 payment support, Radius network RPC endpoints, and faucet URLs. Customize the agent name with `AGENT_NAME`.

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
| `AGENT_NAME` | Display name across all discovery endpoints. Defaults to `Hermes Agent`. |
| `AGENT_DESCRIPTION` | One-line description published in `agent-card.json`. |
| `DEBUG_SKILLS=1` | Enables a `/debug/skills` endpoint showing the server's runtime state. Off by default. |

## Agent-to-agent (A2A) communication

This template implements the [A2A protocol](https://github.com/a2aproject/A2A), making your Hermes agent a first-class participant in a network of autonomous agents. Any other A2A-compatible agent can discover yours, verify its identity, and delegate tasks to it — without any pre-shared secrets or manual coordination.

Combined with the built-in Radius wallet, this unlocks **agent-to-agent payments**: agents can pay each other for work, request tokens in exchange for services, or settle tasks on-chain as part of a larger workflow. Every agent in this network has a persistent cryptographic identity tied to an Ethereum-compatible wallet, so value and trust travel together.

### How identity works

On first boot, a secp256k1 keypair is derived from `RADIUS_PRIVATE_KEY` (the same key as the Radius wallet). A `did:web` DID is constructed from the public domain (e.g. `did:web:my-agent.railway.app`) and becomes the agent's persistent cryptographic identity. It appears in:

- `/.well-known/did.json` — the W3C DID document, with the agent's public key in JWK format
- `/.well-known/agent-card.json` — in `provider.did`
- `/.well-known/agent-registration.json` — in `did`
- Every JWT this agent issues — as the `iss` claim
- Every startup log — so you can copy it for use as a `TRUSTED_DIDS` value on another agent

The `did:web` method means the DID is resolvable over HTTPS — any agent that knows the domain can fetch `/.well-known/did.json`, retrieve the public key, and verify signatures without any pre-shared secrets.

Because the wallet key and the signing key are the same, one `RADIUS_PRIVATE_KEY` gives you an Ethereum address for payments and a DID for verifiable agent identity.

### JWT gate

All non-discovery endpoints (`/health`, `/debug/skills`, `/a2a`) require a Bearer JWT in the `Authorization` header. The gate accepts:

- **Any cryptographically valid DID JWT** — the caller signs a JWT with their own DID and presents it. In this template the issuer is `did:web`.
- **Self-issued tokens** — tokens issued by `POST /token` on this agent. Always accepted regardless of `TRUSTED_DIDS`.

To restrict access to specific agents, set `TRUSTED_DIDS` to a comma-separated list of allowed DID values. When unset, any agent with a valid DID can call gated endpoints.

#### Issuing tokens via `POST /token`

For callers that don't have their own DID infrastructure, this agent can issue tokens:

```bash
curl -X POST https://your-agent.railway.app/token \
  -H "X-Api-Key: $JWT_API_KEY"
# → { "token": "eyJ..." }
```

Set `JWT_API_KEY` in Railway to enable this endpoint. Leave it unset to disable it entirely.

The returned token is a 24-hour JWT signed by this agent's `did:web` identity. Use it as a Bearer token on any gated endpoint.

#### Signing your own JWT (agent-to-agent)

If the calling agent also runs this template (or uses [agentcommercekit](https://github.com/agentcommercekit/ack)), it can sign its own JWT with a DID-compatible keypair:

```ts
import { createJwt, createJwtSigner, generateKeypair, createDidKeyUri, hexStringToBytes } from "agentcommercekit"

const keypair = await generateKeypair("secp256k1", hexStringToBytes(process.env.RADIUS_PRIVATE_KEY))
const did = createDidKeyUri(keypair)
const signer = createJwtSigner(keypair)

const now = Math.floor(Date.now() / 1000)
const token = await createJwt(
  { sub: "my-agent", iat: now, exp: now + 3600 },
  { issuer: did, signer },
  { alg: "ES256K" }
)
// → use as Bearer token on POST /a2a
```

To allow this agent to call yours, add its DID (logged at startup) to your `TRUSTED_DIDS`.

### Reducing "dangerous command" prompts during A2A

The container bootstraps a default Claude permission allowlist in `${HOME}/.claude/settings.json` so routine A2A commands do not require manual confirmation each turn. It includes:

- `curl` calls to discovery endpoints, `/token`, and `/a2a`
- `python3 /app/scripts/agent_server/gen_jwt.py` to generate JWTs with the correct ES256K signature format

### A2A endpoint (`POST /a2a`)

The `/a2a` endpoint accepts [A2A](https://github.com/a2aproject/A2A) JSON-RPC 2.0 requests and now supports two execution modes controlled by `A2A_MODE`.
Request/response validation and JSON-RPC envelope shaping are implemented with the official [`a2a-sdk`](https://github.com/a2aproject/a2a-python) models for protocol compliance.

**Direct mode (`A2A_MODE=direct`)**

- `message/send` returns an inline completed result from Hermes.
- `message/stream` returns SSE events with incremental text deltas and a final completion event.
- `context_id` is forwarded as `X-Hermes-Session-Id` for session continuity.

**Delegated mode (`A2A_MODE=delegated`)**

- Preserves the existing webhook handoff behavior (`/a2a` → Hermes `/webhooks/a2a`).
- `message/send` returns `TASK_STATE_SUBMITTED`.
- `message/stream` is not supported in delegated mode.

**Auto mode (`A2A_MODE=auto`, default)**

- Uses direct handling when `HERMES_API_KEY` is configured.
- Falls back to delegated handling for `message/send` otherwise.

```bash
curl -X POST https://your-agent.railway.app/a2a \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "message/send",
    "params": {
      "message": {
        "role": "ROLE_USER",
        "parts": [{ "text": "Summarize the latest news about AI agents" }]
      }
    }
  }'
```

Example response in delegated mode:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "context_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": {
      "state": "TASK_STATE_SUBMITTED",
      "timestamp_ms": 1712345678000
    }
  }
}
```

In direct mode, the response contains completed text content directly from Hermes.

The caller's DID (from the JWT `iss` claim) is forwarded to Hermes in the webhook payload as `issuer_did`, so Hermes can identify which agent sent the task.

### Configuring direct and delegated bridges

#### Direct bridge variables

| Variable | Description |
|---|---|
| `A2A_MODE` | `auto` (default), `direct`, or `delegated`. |
| `HERMES_API_KEY` | Required for direct mode. Used as Bearer auth to Hermes OpenAI endpoint. |
| `HERMES_URL` | Hermes OpenAI-compatible base URL. Default: `http://127.0.0.1:8642`. |
| `HERMES_MODEL` | Model name for direct bridge requests. Default: `hermes-agent`. |
| `HERMES_TIMEOUT` | Direct bridge timeout in seconds. Default: `120`. |
| `A2A_PUBLIC_URL` | Optional URL used in attachment links. Defaults to service base URL. |
| `A2A_FILE_SERVE_PATHS` | Optional comma-separated file roots allowed for `/files/{path}` serving. |

#### Delegated webhook bridge

The `/a2a` endpoint works by forwarding tasks to Hermes's internal webhook server over HMAC-authenticated HTTP. To enable it:

**1. Set `WEBHOOK_SECRET` in Railway** (any strong random string):

```
WEBHOOK_SECRET=your-random-secret-here
```

**2. Set `WEBHOOK_ENABLED=true` in Railway.**

**3. Add an `a2a` route to Hermes `config.yaml`:**

```yaml
platforms:
  webhook:
    extra:
      routes:
        a2a:
          events: ["*"]
          secret: "${WEBHOOK_SECRET}"
          prompt: "{text}"
```

This tells Hermes to accept webhook POSTs at `/webhooks/a2a` and use the `text` field from the payload as the prompt. The `secret` must match `WEBHOOK_SECRET`.

Once configured, `POST /a2a` → Hermes is live. The agent card at `/.well-known/agent-card.json` will automatically advertise `capabilities.push_notifications: true`.

### Agent-to-agent payments

Because every Hermes agent in this template has both a `did:web` identity and a Radius wallet derived from the same key, agents can pay each other for work as part of any A2A conversation.

**How it works:**

1. Agent A calls Agent B via `POST /a2a` with a task (e.g. "run this analysis and invoice me")
2. Agent B completes the task and responds with its wallet address and a requested amount
3. Agent A uses its built-in wallet skill to send SBC tokens to Agent B on-chain
4. Either agent can verify settlement by checking the on-chain balance

No payment processor, no API keys for billing, no off-chain accounting — just two agents with wallets settling directly on the Radius testnet.

To connect two agents for both task delegation and payments:

| Agent | Required env vars |
|---|---|
| Calling agent (A) | `A2A_PEER_URL=https://<agent-b-domain>`, `TRUSTED_DIDS=did:web:<agent-b-domain>` |
| Receiving agent (B) | `WEBHOOK_ENABLED=true`, `WEBHOOK_SECRET=<shared-secret>`, `TRUSTED_DIDS=did:web:<agent-a-domain>` |

Each agent's DID and wallet address are logged at startup and available at `/.well-known/did.json` and `/.well-known/agent-registration.json`.

### A2A variables

| Variable | Description |
|---|---|
| `A2A_MODE` | `auto` (default), `direct`, or `delegated`. Controls routing behavior for `/a2a`. |
| `HERMES_API_KEY` | Required for direct mode. Hermes OpenAI-compatible API key. |
| `HERMES_URL` | Hermes OpenAI-compatible base URL. Defaults to `http://127.0.0.1:8642`. |
| `HERMES_MODEL` | Model name for direct bridge requests. Defaults to `hermes-agent`. |
| `HERMES_TIMEOUT` | Direct bridge timeout in seconds. Defaults to `120`. |
| `A2A_PUBLIC_URL` | Optional public URL used for generated attachment links. |
| `A2A_FILE_SERVE_PATHS` | Optional comma-separated list of file roots allowed for `/files/{path}` serving. |
| `WEBHOOK_SECRET` | Required to enable the A2A bridge. HMAC key for Hermes webhook authentication. |
| `WEBHOOK_ENABLED` | Set to `true` to start the Hermes webhook server. |
| `WEBHOOK_PORT` | Hermes webhook server port. Defaults to `8644`. |
| `JWT_API_KEY` | Enables `POST /token`. Callers present this key to receive a signed JWT. |
| `TRUSTED_DIDS` | Comma-separated DID allowlist. When set, only these DIDs (plus self-issued tokens) can call gated endpoints. Leave unset to accept any valid DID JWT. |
| `A2A_PEER_URL` | URL of a pre-configured peer agent. Used by the `a2a-comms` skill as the default call target. |
| `A2A_PEER_API_KEY` | API key for the peer's `/token` endpoint, if they require one. |

---

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
5. Initializes Radius wallet if not already done (generates key, calls faucet).
6. Copies all `skills/*.md` files to `${HERMES_HOME}/skills/` (overwrites on each boot).
7. Copies skills with `published: true` frontmatter to `${HERMES_HOME}/skills/` in `name/SKILL.md` structure.
8. Starts the FastAPI agent server in background (binds `PORT`).
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
