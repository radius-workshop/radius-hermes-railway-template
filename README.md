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
- Built-in discovery aggregation tool via `get_agent_info`
- Built-in deterministic ERC-8004 registry tools for reading and writing Radius agent registrations
- Built-in outbound A2A helper via `send_a2a_message` with sender-side correlation logging
- Railway-friendly observability: structured JSON logs from the agent server plus forwarded Hermes harness log files

## How it works

1. You configure required variables in Railway.
2. On first boot, entrypoint initializes Hermes under `/data/.hermes`.
3. On future boots, the same persisted state is reused.
4. Container starts the Python/FastAPI agent server and `hermes gateway` in parallel.

## Quick start (deploy from CLI)

If you're deploying manually with the Railway CLI:

```bash
# 1. Create a Railway project and add a volume mounted at /data
# 2. Link this repo to the project
railway link

# 3. Set required env vars (at minimum: a provider + a platform)
railway variables --set ANTHROPIC_API_KEY=sk-ant-...
railway variables --set TELEGRAM_BOT_TOKEN=123456:ABC...

# 4. Run the pre-deploy check, then deploy
./deploy.sh
```

`deploy.sh` validates that the required env vars are set in your linked Railway project before uploading anything, so you get a clear error locally instead of a crash loop in production.

If you want a full clean slate, run:

```bash
./deploy.sh --reset-state
```

That clears the persisted Railway volume paths used by Hermes before deploying:

- `/data/.hermes`
- `/data/workspace`
- `/data/.claude`

This resets agent memory, sessions, pairing state, ByteRover state, workspace files, and the persisted Radius wallet.

## Example prompts

As soon as the agent is live, these are good first prompts to try in chat.

The bundled public Radius-facing skills include the template-owned skills plus any vendored upstream Radius marketplace skills that are present in the deployed image and marked `published: true`:

- `radius-wallet`
- `a2a-comms`
- `registering-agent`

`radius-wallet`, `a2a-comms`, and `registering-agent` are template-owned. Additional Radius marketplace skills are sourced from the vendored upstream Radius skills repo at deploy time and retain their upstream names.

### Radius wallet and funding

- *"What is my wallet address?"*
- *"Check my Radius wallet balance."*
- *"How much SBC and RUSD do I have right now?"*
- *"Show me my wallet address and give me the testnet explorer link."*
- *"Do I already have testnet funds, or do I need to use the faucet?"*
- *"How do I get more Radius testnet funds?"*
- *"Explain the difference between SBC and RUSD in this wallet."*

### Radius transactions

- *"Send 0.001 SBC to 0x1234... and show me the transaction hash."*
- *"Before sending, tell me if I have enough balance to send 5 SBC."*
- *"What would happen if I tried to send more SBC than I have?"*
- *"Check the status of this Radius transaction: 0xabc..."*

### Radius developer questions

- *"What is Radius, and what can this agent do with it?"*
- *"Give me the Radius Testnet chain ID, RPC URL, and explorer."*
- *"How is Radius different from Ethereum for app developers?"*
- *"What fee assumptions should I avoid when building on Radius?"*
- *"Show me the correct network settings for Radius Testnet and mainnet."*

### Agent-to-agent workflows

- *"What is this agent's DID?"*
- *"Show me this agent's public discovery information."*
- *"What can another A2A agent learn from this agent card?"*
- *"Send a task to https://<other-agent>/a2a asking it to introduce itself."*
- *"Use the outbound A2A tool to ask the peer agent what skills it has."*
- *"Continue the existing A2A conversation with the peer agent and ask for a status update."*

### ERC-8004 registration workflows

- *"Show me the current ERC-8004 registry stats on Radius testnet."*
- *"Read the registration for agent 0 on Radius testnet."*
- *"List all registered agents on Radius testnet."*
- *"Register this agent on ERC-8004 using the current wallet and DID."*
- *"Update agent 2's ERC-8004 registration with a new DID and services map."*

### Payments between agents

- *"Ask the peer agent for its wallet address."*
- *"Send Agent 2 a small amount of SBC on testnet and tell me the tx hash."*
- *"Delegate a task to the peer agent, then summarize the A2A correlation ids you used."*
- *"Explain how an A2A task id, message id, and context id relate to each other here."*

### Memory and operator context

- *"What durable things can you remember between sessions?"*
- *"Remember that this wallet belongs to the demo operator."*
- *"Record this transaction and describe why it happened."*

### Optional Linear prompts

If `LINEAR_API_KEY` is set, these are useful immediately:

- *"List my Linear teams."*
- *"Show my current Linear projects."*
- *"Create a Linear issue for improving Railway observability."*
- *"Summarize open issues related to A2A or logging."*

## Railway deploy instructions

In Railway Template Composer:

1. Add a volume mounted at `/data`.
2. Deploy as a worker service.
3. Set only the variables you actually need (see below).

Template defaults (already included in `railway.toml`):

- `HERMES_HOME=/data/.hermes`
- `HOME=/data`
- `MESSAGING_CWD=/data/workspace`
- `LLM_MODEL=openai/gpt-5.4-nano`

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
LLM_MODEL=openai/gpt-5.4-nano
```

Use any model ID supported by your provider. OpenRouter model IDs look like `openai/gpt-5.4-nano` or `openai/gpt-4o`.

## Radius wallet

This template includes a built-in Radius Testnet wallet. On first boot, the entrypoint:

1. Generates a private key (or uses `RADIUS_PRIVATE_KEY` if you set one).
2. Persists the key and address under `/data/.hermes/.radius/`.
3. Requests SBC testnet tokens from the Radius faucet.
4. Installs a wallet skill so Hermes knows how to use it.

The agent can then check balances, send SBC tokens, and show explorer links — all via natural language in chat.

The bundled wallet tools now support two wallet providers for wallet actions:

- `local` — the default for every new session, backed by the persisted `RADIUS_PRIVATE_KEY`
- `para` — an optional Para-backed operator wallet for session-scoped wallet actions

This provider choice only affects wallet actions. The agent's public wallet identity, DID/JWT auth, homepage wallet summary, and ERC-8004 identity remain pinned to the local wallet.

## ERC-8004 registry tools

This template now includes a bundled `erc8004-registry` plugin plus a lightweight `registering-agent` skill.

Use this interface for ERC-8004 work instead of temporary scripts. The plugin exposes deterministic tools for:

- reading one registration
- listing live registrations from the registry contract
- inspecting registry stats
- registering the current agent from defaults
- registering a new agent
- updating an existing agent URI

The plugin ships with checked-in Radius network constants for `testnet` and `mainnet`. `testnet` is enabled now and uses the deployed registry at `0x5cd923Ce1244d5498Bf3f9E0F3a374C2567F1A31` on chain `72344`.

The canonical registration shape used by both the plugin and `/.well-known/agent-registration.json` is:

```json
{
  "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
  "name": "Hermes Agent",
  "description": "A natural language description of the agent",
  "image": "https://example.com/agent.png",
  "services": [
    {
      "name": "web",
      "endpoint": "https://agent.example/"
    },
    {
      "name": "A2A",
      "endpoint": "https://agent.example/.well-known/agent-card.json",
      "version": "0.3.0"
    },
    {
      "name": "DID",
      "endpoint": "did:web:agent.example",
      "version": "v1"
    }
  ],
  "x402Support": false,
  "active": true,
  "registrations": [],
  "supportedTrust": ["reputation"]
}
```

The plugin normalizes this JSON and encodes it as a `data:application/json;base64,...` URI before submitting the transaction.

For the common case, use `erc8004_register_self` instead of hand-constructing a full `registration` object. It derives `web`, `A2A`, and `DID` service entries from the current agent runtime, but it expects operator-owned metadata like `name`, `description`, `image`, and `supportedTrust` to be supplied either as tool params or env vars. Read tools also return both the raw `token_uri` and `normalized_token_uri` so quoted contract responses are easier to debug.

### Radius variables (all optional)

| Variable | Description |
|---|---|
| `RADIUS_PRIVATE_KEY` | BYO private key (`0x...`). Auto-generated if not set. |
| `RADIUS_WALLET_ADDRESS` | Derived from key automatically. |
| `RADIUS_AUTO_FUND` | Set to `false` to skip faucet on boot. Default: enabled. |
| `PARA_API_KEY` | Optional Para server secret key for the alternate `para` wallet provider. |
| `PARA_SECRET_API_KEY` | Optional alias for `PARA_API_KEY`. |
| `PARA_ENVIRONMENT` | Optional Para environment. `beta` by default, or `prod` / `production`. |
| `PARA_REST_BASE_URL` | Optional explicit Para REST base URL override. |
| `PARA_WALLET_ID` | Optional Para wallet ID to pin the operator wallet if the project has multiple EVM wallets. |

The wallet key is stored at `/data/.hermes/.radius/key` with permissions `600`. It persists across redeploys via the Railway volume.

### Para wallet setup notes

If you want to use the optional `para` wallet provider in this project, a Secret API key alone is not enough. The Para project also needs an existing operator-owned EVM wallet.

Recommended setup:

1. Set `PARA_API_KEY` or `PARA_SECRET_API_KEY` to your Para Secret API key.
2. Create an EVM wallet in the Para project before trying to switch the session wallet provider to `para`.
3. Use `scheme: "DKLS"` for this project's EVM wallet.
4. If the Para project has multiple EVM wallets, also set `PARA_WALLET_ID` so the agent uses the intended operator wallet.

Notes:

- This project treats Para as a wallet provider for wallet actions only. It does not replace the agent's canonical local identity wallet.
- `ED25519` is not the right choice for this EVM wallet flow.
- If no EVM wallet exists in the Para project, the agent will hard-error when a session tries to switch to `para`.

### Wallet commands (via chat)

Once deployed, you can ask the agent:

- *"What is my wallet address?"*
- *"Check my balance"*
- *"Send 10 SBC to 0x..."*
- *"Get testnet tokens"*
- *"Use Para wallet for this session"*
- *"Switch back to local wallet"*
- *"What is my local wallet address?"*
- *"What is my Para wallet address?"*

The preferred interface is the bundled `radius-cast` plugin tools. The underlying wallet bootstrap and fallback scripts live under `/app/scripts/radius/`.

The wallet tool behavior is:

- Every new session defaults to `local`.
- If the user explicitly switches the session to `para`, wallet actions default to the Para wallet for the rest of that session.
- Users can still override per request by explicitly asking for the `local` or `para` wallet.
- If `para` is requested but not configured, the agent returns a hard error instead of silently falling back.

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

## Logging in Railway

This template emits two complementary log streams into Railway:

- **Agent server logs** from `scripts/agent_server/main.py` are written as single-line JSON to stdout/stderr. These cover A2A auth, request validation, direct vs delegated routing, fallback behavior, and request timing.
- **Hermes harness logs** from `${HERMES_HOME}/logs/agent.log` and `errors.log` are tailed by `scripts/entrypoint.sh` and forwarded into Railway output with prefixes like `[hermes:agent.log] ...`.

This split is intentional:

- Railway's Log Explorer can parse the JSON agent-server logs into filterable fields such as `@event`, `@request_id`, `@rpc_id`, `@rpc_method`, `@a2a_mode`, `@issuer_did`, `@context_id`, `@status_code`, and `@duration_ms`.
- Hermes's own file logs provide the higher-level harness/tool execution trail that is often missing from plain HTTP access logs.

Useful Railway filters after deploy:

- `@event:a2a.request`
- `@event:a2a.direct`
- `@event:a2a.delegated`
- `@event:auth.jwt_rejected`
- `@request_id:<id>`
- `@context_id:<context-id>`
- `@issuer_did:did:web:...`
- `@outcome:error`

If you want to disable Hermes log-file forwarding, set:

```bash
HERMES_FORWARD_LOG_FILES=false
```

`gateway.log` forwarding is disabled by default because Hermes often mirrors the same gateway events into both `agent.log` and `gateway.log`, which creates duplicate Railway entries. If you explicitly want the extra stream, set:

```bash
HERMES_FORWARD_GATEWAY_LOG=true
```

The FastAPI server also disables uvicorn access logs by default so Railway shows the structured request log line instead of both the structured line and the plain `INFO ... "GET /health"` access line.

References:

- [Railway Logs documentation](https://docs.railway.com/observability/logs)
- [Hermes CLI log files documentation](https://hermes-agent.nousresearch.com/docs/reference/cli-commands?_highlight=logging#log-files)

### Endpoints

| Path | Auth | Spec | Description |
|---|---|---|---|
| `/.well-known/did.json` | Public | [W3C DID](https://www.w3.org/TR/did-core/) | DID document for this agent's `did:web` identity — public key, verification methods |
| `/.well-known/agent-card.json` | Public | [A2A](https://github.com/a2aproject/A2A) | A2A agent card — identity, skills, supported interfaces, auth scheme |
| `/.well-known/agent-registration.json` | Public | [ERC 8004](https://eips.ethereum.org/EIPS/eip-8004) | ERC-8004 self-registration profile and advertised services |
| `/.well-known/agent-skills/index.json` | Public | [Cloudflare Agent Skills Discovery RFC](https://github.com/cloudflare/agent-skills-discovery-rfc) | Index of published skills with digests and URLs |
| `/.well-known/agent-skills/:name/SKILL.md` | Public | Cloudflare Agent Skills Discovery RFC | Individual skill document |

**`agent-card.json`** is the A2A discovery document. Other agents fetch it to learn how to authenticate and what this agent can do. It includes the agent's `did:web` identity (derived from `RADIUS_PRIVATE_KEY`), the `POST /a2a` interface, and the `bearer_jwt` security scheme. Skills are pulled live from the skill discovery index.

**`agent-registration.json`** advertises this agent's ERC-8004 self-registration profile. It follows the `registration-v1` schema, derives `web`, `A2A`, and `DID` services from the running agent, and returns `503` with missing-field guidance until required operator metadata such as `AGENT_NAME`, `AGENT_DESCRIPTION`, `AGENT_IMAGE`, and `AGENT_SUPPORTED_TRUST` is configured.

**`agent-skills/index.json`** lets other agents and tools enumerate what this agent can do. Each entry includes the skill name, description, a URL to fetch the full skill document, and a SHA-256 content digest so consumers can detect updates.

In this template, the template-owned Radius-facing skills are installed into the Hermes catalog-style `radius` bucket under `${HERMES_HOME}/skills/radius/...`. Vendored marketplace skills are discovered through Hermes `skills.external_dirs` using their upstream names:

- `radius-wallet`
- `a2a-comms`
- `registering-agent`

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
| `AGENT_IMAGE` | Required for ERC-8004 self-registration and `agent-registration.json`. Public image URL. |
| `AGENT_SUPPORTED_TRUST` | Required for ERC-8004 self-registration when not passed directly. Comma-separated trust modes such as `reputation,crypto-economic`. |
| `AGENT_X402_SUPPORT` | Optional ERC-8004 x402 support flag. Defaults to `false`. |
| `AGENT_ACTIVE` | Optional ERC-8004 active flag. Defaults to `true`. |
| `AGENT_EMAIL` | Optional email service endpoint advertised in ERC-8004 registration. |
| `AGENT_ENS` | Optional ENS name advertised in ERC-8004 registration. |
| `AGENT_A2A_VERSION` | Optional A2A version string for ERC-8004 registration. Defaults to `0.3.0`. |
| `AGENT_MCP_ENDPOINT` | Optional MCP service endpoint advertised in ERC-8004 registration. |
| `AGENT_MCP_VERSION` | Optional MCP version string. |
| `AGENT_OASF_ENDPOINT` | Optional OASF service endpoint. |
| `AGENT_OASF_VERSION` | Optional OASF version string. |
| `AGENT_OASF_SKILLS` | Optional comma-separated OASF skills list. |
| `AGENT_OASF_DOMAINS` | Optional comma-separated OASF domains list. |
| `DEBUG_SKILLS=1` | Enables a gated `/debug/skills` endpoint showing config external dirs, vendored skill scans, local skills, and the public index. Off by default. |
| `EXPECTED_VENDORED_SKILLS` | Optional comma-separated list of upstream vendored skill names expected in the image. Logs a warning if any are missing. |
| `STRICT_VENDORED_SKILLS` | When `true`, fail boot if any `EXPECTED_VENDORED_SKILLS` are missing. Defaults to `false`. |
| `VENDORED_SKILLS_SOURCE` | Optional override for the vendored Radius skills repo root. Defaults to `/app/vendor/radius-skills`. |

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

### Aggregating discovery metadata

The bundled `agent-info` plugin exposes a `get_agent_info` tool that retrieves the public discovery bundle for an agent in one call:

```text
get_agent_info()
get_agent_info({"agent":"https://other-agent.example"})
get_agent_info({"agent":"did:web:other-agent.example","include_skill_docs":false})
```

It aggregates:

- `/.well-known/agent-card.json`
- `/.well-known/agent-skills/index.json`
- published `SKILL.md` documents
- `/.well-known/agent-registration.json`
- `/.well-known/did.json`

If no `agent` argument is supplied, the tool defaults to the current agent.

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

For outbound A2A calls initiated by this agent, prefer the bundled `send_a2a_message` tool rather than raw `curl`. It records sender-side correlation fields in Hermes logs, including:

- `rpc_id`
- `a2a_message_id`
- `context_id`
- returned `a2a_task_id`
- remote agent URL

Example:

```text
send_a2a_message({"agent":"https://other-agent.example","task":"Run this analysis"})
```

For long-running conversations, `send_a2a_message` also accepts optional managed-session fields:

```text
send_a2a_message({
  "agent":"https://other-agent.example",
  "task":"Start the design review",
  "goal":"Iterate on the architecture until the proposal is complete",
  "auto_continue":true,
  "max_turns":200
})
```

When `auto_continue` is enabled, the local agent server persists an A2A session under `${HERMES_HOME}/a2a-sessions`, tracks the shared `context_id`, records inbound/outbound turns, and can keep generating the next local turn automatically instead of treating the exchange as one-shot.
When the same host thread keeps talking to the same remote agent, the outbound helper now reuses the active managed session by default instead of creating a fresh `session_id` on every turn.

Each managed session now also stores Telegram/Discord-ready per-turn cards in its session JSON and internal session API payloads:

```json
{
  "latest_card": {
    "title": "Turn 4/10 • Peer Agent",
    "body": "Use a manual oracle first so pricing stays deterministic during testing.",
    "footer": "Preparing next turn • Context 93f062e91",
    "text": "Turn 4/10 • Peer Agent\n\nUse a manual oracle first so pricing stays deterministic during testing.\n\nPreparing next turn • Context 93f062e91"
  }
}
```

This renderer layer is intended for host-platform mirroring in Telegram or Discord so the user sees clean per-turn cards instead of a raw transcript dump.
The outbound `send_a2a_message` tool also surfaces a `user_update` string plus the resolved `session` payload so host agents can reply with that polished text directly.

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
- Managed A2A sessions work across both modes: direct replies can continue inline, while delegated peers can re-enter the same session by sending follow-up turns on the same `context_id`.

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
| `HERMES_API_KEY` | Required for direct mode. Used as Bearer auth to Hermes OpenAI endpoint. If unset, the bridge also accepts `API_SERVER_KEY`. |
| `HERMES_URL` | Hermes OpenAI-compatible base URL. Default: `http://127.0.0.1:8642`. |
| `HERMES_MODEL` | Model name for direct bridge requests. Default: `hermes-agent`. |
| `HERMES_TIMEOUT` | Direct bridge timeout in seconds. Default: `120`. |
| `A2A_PUBLIC_URL` | Optional URL used in attachment links. Defaults to service base URL. |
| `A2A_FILE_SERVE_PATHS` | Optional comma-separated file roots allowed for `/files/{path}` serving. |
| `A2A_SESSION_TICK_SECONDS` | Optional poll interval for the managed A2A session worker. Default: `2.5`. |

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
| `HERMES_API_KEY` | Required for direct mode. Hermes OpenAI-compatible API key. If unset, the bridge also accepts `API_SERVER_KEY`. |
| `HERMES_URL` | Hermes OpenAI-compatible base URL. Defaults to `http://127.0.0.1:8642`. |
| `HERMES_MODEL` | Model name for direct bridge requests. Defaults to `hermes-agent`. |
| `HERMES_TIMEOUT` | Direct bridge timeout in seconds. Defaults to `120`. |
| `A2A_PUBLIC_URL` | Optional public URL used for generated attachment links. |
| `A2A_FILE_SERVE_PATHS` | Optional comma-separated list of file roots allowed for `/files/{path}` serving. |
| `WEBHOOK_SECRET` | Required to enable the A2A bridge. HMAC key for Hermes webhook authentication. |
| `WEBHOOK_ENABLED` | Set to `true` to start the Hermes webhook server. |
| `WEBHOOK_PORT` | Hermes webhook server port. Defaults to `8644`. |
| `JWT_API_KEY` | Enables `POST /token`. Callers present this key to receive a signed JWT. `JWT_EXCHANGE_KEY` is also accepted for backward compatibility. |
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

The `radius-wallet.md` skill is already included and tells the agent to prefer the bundled Radius wallet tools, with script fallback where needed.

Radius-maintained marketplace skills are also vendored from `https://github.com/radiustechsystems/skills` at image build time. On boot, the template scans that vendored repo for every directory containing `SKILL.md`, derives the necessary `skills.external_dirs`, and exposes those skills to Hermes as read-only external skills without copying them into `${HERMES_HOME}/skills/`.

The template also includes an opinionated ByteRover memory skill and project instructions. When ByteRover is enabled, the intended usage is:

- organize memory by session date
- save only top-level, durable topics
- track wallet addresses, descriptions, and important transactions as structured long-term memory

### Context files and cross-agent compatibility

Hermes supports repo context files. Per the Hermes docs, project context is loaded by priority from `.hermes.md` / `HERMES.md`, then `AGENTS.md`, then `CLAUDE.md`, then `.cursorrules`.

This template ships both:

- `HERMES.md` for Hermes-native project context
- `AGENTS.md` for Codex/Claude-style agents that look for `AGENTS.md`

That gives you the same core instructions across Hermes and non-Hermes coding agents while keeping `HERMES.md` as the primary Hermes context file.

At runtime, gateway sessions start in `MESSAGING_CWD` (`/data/workspace` by default), not `/app`. To make the bundled project context discoverable immediately, the entrypoint links `HERMES.md`, `.hermes.md`, `AGENTS.md`, `README.md`, `skills/`, `plugins/`, and `scripts/` into the workspace root on boot.

`HERMES.md`, `.hermes.md`, and `AGENTS.md` are template-owned and are force-updated on every boot so the deployed agent always uses the repo's current instructions.

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
6. Copies all local `skills/*.md` files to `${HERMES_HOME}/skills/` (overwrites on each boot).
7. Scans the vendored `radiustechsystems/skills` repo for all upstream skill directories, writes a discovery manifest, registers the derived parent roots as Hermes `skills.external_dirs`, and optionally warns or fails if `EXPECTED_VENDORED_SKILLS` are missing.
8. Copies bundled plugins from `plugins/*` to `${HERMES_HOME}/plugins/`.
9. Enables the bundled `gen-jwt`, `a2a-send`, and `radius-cast` plugin toolsets so A2A auth, outbound A2A calls, and Radius wallet tools are available immediately.
10. Links `HERMES.md`, `.hermes.md`, `AGENTS.md`, `README.md`, `skills/`, `plugins/`, and `scripts/` into `${MESSAGING_CWD}` so gateway sessions see the bundled project context immediately. The three context files are force-overwritten on each boot.
11. Copies published skills to `${HERMES_HOME}/well-known-skills/` for skill discovery endpoints.
12. Starts the FastAPI agent server in background (binds `PORT`).
13. Starts log forwarders for `${HERMES_HOME}/logs/agent.log` and `errors.log` unless `HERMES_FORWARD_LOG_FILES=false`. `gateway.log` is opt-in via `HERMES_FORWARD_GATEWAY_LOG=true`.
14. Starts `hermes gateway` and supervises it alongside the agent server so Railway sees both logging layers.

## Troubleshooting

**`ValueError: invalid literal for int() with base 10: ""`**
An optional integer variable (e.g. `HERMES_MAX_ITERATIONS`) is set in Railway with an empty value. Remove it from Railway Variables entirely — do not leave optional variables set to empty strings.

**`401 Missing Authentication header`**
Provider/key mismatch. Set `HERMES_INFERENCE_PROVIDER` explicitly (e.g. `openrouter`) to avoid auto-selection picking the wrong provider.

**Bot connected but no replies**
Check `TELEGRAM_ALLOWED_USERS` / `DISCORD_ALLOWED_USERS` / `SLACK_ALLOWED_USERS`. Your user ID must be in the list, or set `GATEWAY_ALLOW_ALL_USERS=true` (not recommended for public bots).

**Railway only shows HTTP access lines**
Make sure you're on a deployment with the updated entrypoint. The template now forwards Hermes log files into Railway output and emits structured JSON from the agent server. Search Railway logs for `@event:a2a.request` or text like `[hermes:agent.log]`.

**Data lost after redeploy**
Verify the Railway volume is mounted at `/data` in your service settings. Without the volume, state is lost on every deploy.

**Radius wallet not initialized**
Check deploy logs for `[radius]` lines. If wallet initialization appears to have failed, SSH in and run:
```bash
python3 /app/scripts/radius/wallet_init.py
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
