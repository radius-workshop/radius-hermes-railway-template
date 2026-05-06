# Hermes Agent Railway Template

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-railway-template?referralCode=uTN7AS&utm_medium=integration&utm_source=template&utm_campaign=generic)

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) to Railway as a worker service with persistent state.

This template is worker-only: setup and configuration are done through Railway Variables, then the container bootstraps Hermes automatically on first run.

## Radius skills + tools bootstrap

Agents created from this template automatically bootstrap the Radius skills repo snapshot into persistent external skills storage at startup:

- `/app/vendor/radius-skills` is cloned into the image from `https://github.com/radiustechsystems/skills.git`
- on first boot, `scripts/entrypoint.sh` copies that snapshot to `RADIUS_SKILLS_DIR` (default `/data/.hermes/external-skills/radius-skills`)
- Hermes `skills.external_dirs` is populated with the discovered Radius skill roots
- the Radius `radius-cast` Hermes plugin remains enabled so the agent has deterministic Radius wallet tools

This means new Railway-template agents should see both the portable skills and the tool/plugin surface:

- skills: `radius-dev`, `x402`, `dripping-faucet`, `radius-agent-ops`
- tools: `radius_wallet_address`, `radius_balance`, `radius_send_sbc`, `radius_send_rusd`, `radius_tx_status`, `radius_chain_info`

Local verification:

```bash
RADIUS_SKILLS_REPO_CHECKOUT=/app/skills python3 -m unittest tests/test_radius_skills_bootstrap.py -v
```

This test intentionally does not cover the GitHub Action / webhook subscriber notification path.

## What You Get

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
- updating an existing agent URI with a complete replacement registration
- patching an existing registration while preserving current metadata
- adding canonical web/A2A/DID aliases plus a GoDaddy ANS pointer

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
      "endpoint": "https://agent.example/a2a",
      "metadata": "https://agent.example/.well-known/agent-card.json",
      "version": "0.3.0"
    },
    {
      "name": "DID",
      "endpoint": "did:web:agent.example",
      "version": "v1"
    }
  ],
  "aliases": [],
  "x402Support": false,
  "active": true,
  "registrations": [],
  "externalRegistrations": [],
  "supportedTrust": ["reputation"]
}
```

The plugin normalizes this JSON and encodes it as a `data:application/json;base64,...` URI before submitting the transaction.

For the common case, use `erc8004_register_self` instead of hand-constructing a full `registration` object. It derives `web`, `A2A`, and `DID` service entries from the current agent runtime, but it expects operator-owned metadata like `name`, `description`, `image`, and `supportedTrust` to be supplied either as tool params or env vars. Read tools also return both the raw `token_uri` and `normalized_token_uri` so quoted contract responses are easier to debug.

For partial metadata updates, use `erc8004_patch_agent_registration` with `dry_run=true` first. It fetches the current registration, merges `services_add`, `services_update`, `aliases_add`, `externalRegistrations_add`, and `fields`, deduplicates entries, validates the full result, and returns a data URI plus structural diff without submitting a transaction. Use `erc8004_add_ans_pointer` for the common GoDaddy ANS flow; it adds web/A2A/DID aliases, an `ANS` service, and an `externalRegistrations[]` entry.

Safe update workflow: dry-run the intended specialized tool, inspect the diff, submit that same tool once, then verify with on-chain readback and tx status. For GoDaddy ANS/domain updates, use `erc8004_add_ans_pointer`; do not use generic patch or full replacement tools as probes after the ANS dry-run already shows the intended diff. Keep `erc8004_update_agent_uri` for deliberate full replacement writes only, with `replace_full_registration=true`.

### ERC-8004 variables

| Variable | Description |
|---|---|
| `ERC8004_NETWORK` | Defaults to `testnet`. |
| `ERC8004_TESTNET_RPC_URL` | Defaults to `https://rpc.testnet.radiustech.xyz`. |
| `ERC8004_TESTNET_REGISTRY` | Defaults to `0x5cd923Ce1244d5498Bf3f9E0F3a374C2567F1A31`. |
| `ERC8004_TESTNET_EXPLORER_URL` | Defaults to `https://testnet.radiustech.xyz`. |
| `ERC8004_GAS_LIMIT` | Defaults to `2000000`. |
| `AGENT_ERC8004_ID` | Optional on-chain token ID for public metadata. |
| `AGENT_ERC8004_REGISTRY` | Optional registry ref override for public metadata. |
| `AGENT_ANS_NAME` | Optional ANS pointer, e.g. `ans://v1.0.0.agent0.72344.xyz`. |
| `AGENT_ANS_AGENT_ID` | Optional GoDaddy ANS UUID. |
| `AGENT_ANS_HOST` | Optional host, e.g. `agent0.72344.xyz`. |
| `AGENT_ANS_STATUS` | Optional ANS lifecycle status. |

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

## Agent server

The FastAPI agent server lives in [scripts/agent_server](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server) and owns:

- the public discovery endpoints under `/.well-known/*`
- `did:web` identity and JWT auth
- `POST /token`
- `POST /a2a`
- the public homepage at `/`
- structured agent-server logging

The implementation details now live in the local agent-server README:

- [scripts/agent_server/README.md](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/README.md)

That file contains:

- endpoint inventory and auth behavior
- discovery, registration, and skill publishing rules
- A2A modes, variables, and delegated webhook setup
- observability notes
- local mock-data workflow for fast homepage/UI iteration

At the repo level, the main thing to know is that this template exposes a first-class A2A surface with Radius-backed identity and wallet state. Other agents can discover it via `agent-card.json`, verify it via `did.json`, inspect capabilities via `agent-skills/index.json`, and interact with it over `POST /a2a`.

The bundled `agent-info` plugin is the easiest way to aggregate a full public discovery bundle for any compatible agent:

```text
get_agent_info()
get_agent_info({"agent":"https://other-agent.example"})
get_agent_info({"agent":"did:web:other-agent.example","include_skill_docs":false})
```

If you are actively changing the homepage or agent-server behavior, work from the dedicated local docs in `scripts/agent_server/README.md` instead of this top-level README.

---

## Customizing the agent with instructions

### Skills (agent knowledge files)

Skills are Markdown files that tell Hermes about available capabilities. They live at `$HERMES_HOME/skills/` and are loaded automatically per session.

Any `.md` file you place in the `skills/` directory of this repo will be copied to the Hermes skills directory on every boot. To add your own instructions:

1. Create a file like `skills/my-instructions.md`.
2. Write instructions in plain Markdown — what the agent should know, how to behave, what commands to run.
3. Redeploy. The skill will be installed on next boot.

The `radius-wallet.md` skill is already included and tells the agent to prefer the bundled Radius wallet tools, with script fallback where needed.

The `using-godaddy.md` skill is also included. It tells Hermes to route GoDaddy domain availability and suggestion requests to the configured GoDaddy MCP server, to use `godaddy_dns_set_records` for setting DNS records on a known GoDaddy-managed domain, and to route Agent Name Service registry requests to the local `godaddy-ans` plugin tools. GoDaddy ANS defaults to production; set `GODADDY_ANS_ENV=ote` only when OTE is explicitly required. For example, ANS registry searches should call `godaddy_ans_search` directly instead of inspecting plugin files, running Python scripts, installing packages, or reading GoDaddy API secrets in a terminal. ANS registration uses `godaddy_ans_prepare_registration` to inspect the Swagger-aligned payload and CSRs, then `godaddy_ans_register` to submit once the agent host, endpoint URLs, and domain-validation prerequisites are correct.

Radius-maintained marketplace skills are seeded from `https://github.com/radiustechsystems/skills` at image build time, then managed at runtime as one persistent Hermes external directory (`RADIUS_SKILLS_DIR`, default `/data/.hermes/external-skills/radius-skills`). On boot, the template scans that directory for every `SKILL.md`, derives `skills.external_dirs`, and exposes those skills to Hermes as read-only external skills without copying them into `${HERMES_HOME}/skills/`.

### Auto-updating Radius external skills

Hermes local skills remain primary at `${HERMES_HOME}/skills` and are still editable by Hermes. Radius marketplace skills stay external and read-only from Hermes' perspective via `skills.external_dirs`; if a local and external skill share the same name, local wins.

To enable webhook-driven updates for the managed Radius external directory:

1. Set:
   - `RADIUS_SKILLS_AUTO_UPDATE=true`
   - `RADIUS_SKILLS_WEBHOOK_SECRET=<shared-secret>`
   - optional: `RADIUS_SKILLS_REPO`, `RADIUS_SKILLS_BRANCH`, `RADIUS_SKILLS_GITHUB_TOKEN`
2. Configure a GitHub webhook on the source repo:
   - URL: `https://<your-agent-domain>/webhooks/github/radius-skills`
   - Event: **Push**
   - Content type: `application/json`
   - Secret: same value as `RADIUS_SKILLS_WEBHOOK_SECRET`
3. Use internal observability endpoints:
   - `GET /internal/skills/status` (Bearer internal API key)
   - optional manual refresh: `POST /internal/skills/sync` with `{"after":"<commit-sha>"}`.

If `RADIUS_SKILLS_BRANCH` is omitted, empty, `*`, or `any`, the webhook accepts pushes from any branch under `refs/heads/*` and syncs the pushed branch. If it is set to a concrete branch such as `main`, only that branch is accepted.

The webhook itself returns `202 Accepted` because sync happens asynchronously after signature validation. A successful queue response now includes the target repo/ref/SHA plus `delivery_id`, `status_path`, and whether branch handling is `pinned` or `any`. Progress and outcome are emitted to Railway logs as structured events:

- `skills.webhook` for accept/ignore/reject decisions
- `skills.sync.started` when the background sync begins
- `skills.sync.manifest` after validation and manifest generation
- `skills.sync` for final success/error

`GET /internal/skills/status` also exposes the latest delivery id, seen ref/SHAs, active ref, sync start/completion times, last result, manifest root list, and skill counts.

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
7. Ensures the managed Radius external directory exists on persistent storage (`RADIUS_SKILLS_DIR`), bootstraps it from `/app/vendor/radius-skills` when empty (optional), scans all upstream skill directories, writes a discovery manifest, registers the derived parent roots as Hermes `skills.external_dirs`, and optionally warns or fails if `EXPECTED_VENDORED_SKILLS` are missing.
8. Copies bundled plugins from `plugins/*` to `${HERMES_HOME}/plugins/`.
9. Enables every bundled plugin in both `toolsets` and `plugins.enabled`, and removes bundled plugins from any stale `plugins.disabled` entry so persisted Railway config cannot hide newly bundled tools.
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
