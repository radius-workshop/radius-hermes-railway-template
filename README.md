# Hermes Agent Railway Template

[Release v0.1.0](https://github.com/radius-workshop/radius-hermes-railway-template/releases/tag/v0.1.0) · [Release process](RELEASE.md)

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-railway-template?referralCode=uTN7AS&utm_medium=integration&utm_source=template&utm_campaign=generic)

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) to Railway as a worker service with persistent state.

This template is worker-only: setup and configuration are done through Railway Variables, then the container bootstraps Hermes automatically on first run.

## Radius skills + tools bootstrap

Agents created from this template automatically bootstrap a snapshot of the Radius skills, which live under `plugins/radius/skills` in [radiustechsystems/tools](https://github.com/radiustechsystems/tools), into persistent external skills storage at startup:

- `/app/vendor/radius-skills` is cloned into the image from `https://github.com/radiustechsystems/tools.git` at the commit pinned by the `RADIUS_SKILLS_REF` build arg
- on first boot, `scripts/entrypoint.sh` copies that snapshot to `RADIUS_SKILLS_DIR` (default `/data/.hermes/external-skills/radius-skills`)
- a persisted checkout of the archived `radiustechsystems/skills` repo is replaced with the image snapshot on boot; checkouts of any other repository are kept
- Hermes `skills.external_dirs` is populated with the discovered Radius skill roots
- the Radius `radius-cast` Hermes plugin remains enabled so the agent has deterministic Radius wallet tools

This means new Railway-template agents should see both the portable skills and the tool/plugin surface:

- skills: `radius-dev`, `x402`, `dripping-faucet`, `sell-api`
- tools: `radius_wallet_address`, `radius_balance`, `radius_send_sbc`, `radius_tx_status`

Local verification:

```bash
python3 -m unittest discover -s tests -p test_radius_skills_bootstrap.py -v
```

The tests run the real boot discovery and config registration against isolated fixtures and check bundled CLI tool registration. They require no external checkout, network, wallet or credentials.

## What You Get

- Hermes gateway running as a Railway worker
- First-boot bootstrap from environment variables
- Persistent Hermes state on a Railway volume at `/data`
- Telegram, Discord, or Slack support (at least one required)
- Built-in Radius Testnet wallet (instantiated via radius-cli on first boot, auto-funded via faucet)
- Agent discovery layer served at `/.well-known/*` — ERC 8004 registration, Cloudflare agent skills discovery, and A2A agent card
- Agent-to-agent (A2A) communication with two execution modes: direct (inline `message/send` + `message/stream`) and delegated (webhook-backed async submission)
- Persistent cryptographic identity derived from the radius-cli wallet keystore; exported key material is used for JWT and on-chain signing paths
- Built-in discovery aggregation tool via `get_agent_info`
- Built-in deterministic ERC-8004 registry tools for reading and writing Radius agent registrations
- Built-in outbound A2A helper via `send_a2a_message` with sender-side correlation logging
- Built-in KYA spec verification tool via `kya_validate_claims` (fetches issuer JWKS, verifies ES256 signatures, enforces claim shape, audience/environment binding, and jti replay protection)
- Built-in KYA spec minting tool via `generate_kya_token` (signs KYA / PAY / KYA-PAY JWTs with a persistent P-256 keypair; public half published at `/.well-known/jwks.json`)
- Optional KYA signed-claim verification (requires `TRUSTED_KYA_ISSUERS`) at `POST /a2a` via `A2A_AUTH_MODE` (`did_and_kya`, `did_or_kya`, or `kya_only`)
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
railway variables --set HERMES_INFERENCE_PROVIDER=anthropic
railway variables --set LLM_MODEL=<anthropic-model-id>   # the default model is an OpenRouter/OpenAI ID
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

### KYA / KYAPay spec workflows

- *"Verify this `kya+jwt` token from another agent against expected audience `swaps.radius.xyz`."*
- *"Validate this KYA-PAY JWT and tell me whether the signature checks against the issuer JWKS."*
- *"Check this KYA token but only accept it if the issuer is in our trusted allowlist."*
- *"Validate this KYA token with replay protection disabled — I just want to inspect the claims."*
- *"Mint a KYA token for audience https://peer.example so I can prove identity to that agent."*
- *"Generate a signed kya-pay claim for an intended 0.50 USD payment to https://swaps.radius.xyz with stp=coin; do not submit or settle funds."*
- *"What URL should other agents use to fetch our JWKS?"* (Answer: `${PUBLIC_URL}/.well-known/jwks.json`)
- *"Turn on KYA-required mode for inbound A2A requests."* (Set `A2A_AUTH_MODE=did_and_kya` and `TRUSTED_KYA_ISSUERS=https://issuer.example` in Railway, alongside trusted peer DIDs. Use `kya_only` only when trusted KYA issuers should authorize requests without DID authentication.)

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

## Kubernetes (Helm)

The same image also runs on any Kubernetes cluster through the chart in
[deploy/helm/hermes-agent](deploy/helm/hermes-agent/README.md). The chart
deploys a single-replica StatefulSet with a persistent `/data` volume, an
optional egress proxy, an optional Ingress and optional NetworkPolicies. It
uses core Kubernetes objects only. Pods run as uid 10001 with a read-only root
filesystem and pass the `restricted` Pod Security Standard. Credentials come
from a Secret you provide, so any secret manager works.

```sh
kubectl create secret generic hermes-agent \
  --from-literal=OPENROUTER_API_KEY=... --from-literal=TELEGRAM_BOT_TOKEN=...
helm install hermes deploy/helm/hermes-agent -f deploy/helm/hermes-agent/examples/minimal.yaml \
  --set image.repository=<registry>/radius-hermes --set image.tag=<tag>
```

`scripts/test_helm_chart.sh` installs the chart on a local kind cluster and
checks it end to end.

## Railway deploy instructions

In Railway Template Composer:

1. Add a volume mounted at `/data`.
2. Deploy as a worker service.
3. Set only the variables you actually need (see below).

Template defaults (already included in `railway.toml`):

- `HERMES_HOME=/data/.hermes`
- `HOME=/data`
- `MESSAGING_CWD=/data/workspace`

If `LLM_MODEL` is not set, `scripts/entrypoint.sh` writes `openai/gpt-5.4-nano` as the model in `config.yaml`.

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

With the [egress proxy](#egress-proxy-with-secret-injection), set these on the egress service; the agent gets proxy tokens under the same names.

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

### Slack in a shared channel

When the bot sits in a channel with other people (and other bots), these upstream Hermes Slack variables keep it scoped. They are read by the Hermes gateway from the Railway environment:

```
SLACK_ALLOWED_CHANNELS=C0123456789          # only respond in these channel IDs (DMs are exempt)
SLACK_STRICT_MENTION=true                   # every channel message needs a fresh @mention
SLACK_IGNORE_OTHER_USER_MENTIONS=true       # skip messages that open by @mentioning someone else
SLACK_ALLOW_BOTS=mentions                   # other bots can reach it only by @mentioning it
```

`SLACK_ALLOWED_USERS` still applies on top of these. See the [Hermes Slack docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack) for the full list.

For a channel shared with other agents (peer bots), use the Hermes peer-agent profile: `SLACK_REQUIRE_MENTION=true` (the Hermes default), `SLACK_STRICT_MENTION=true`, `SLACK_ALLOW_BOTS=mentions`, and leave `SLACK_ALLOWED_CHANNELS` empty unless you want to restrict channels. Keep these as Railway variables. Don't also set `require_mention`, `allow_bots` or `reaction_triggers` under `slack:` in `config.yaml`: Hermes reads the config value first, so a Railway change would be ignored.

### Slack experience

These Railway variables turn on the Slack-native reply features in Hermes. All are off by default, so an existing deployment behaves the same until you set them. On every boot the entrypoint writes them into `config.yaml`. If you remove a variable it wrote, the setting is removed on the next boot. Set a variable to `none` to clear a value someone wrote into `config.yaml` by hand.

```
SLACK_STREAMING=true              # replies type out live (Slack chat.startStream); Slack only. See the note below
SLACK_RICH_BLOCKS=true            # Block Kit replies: headers, real lists, native tables
SLACK_FEEDBACK_BUTTONS=true       # 👍/👎 under final replies (needs SLACK_RICH_BLOCKS=true)
SLACK_NATIVE_TASK_CARDS=true      # tool calls shown as live Slack task cards
SLACK_SUGGESTED_PROMPTS=default   # 4 Radius starter prompts in the agent DM, or JSON (below)
SLACK_LIVE_STATUS=verb            # status line detail: full (Hermes default) | verb | off
SLACK_RUNTIME_FOOTER=on           # "model · context %" footer; or a list: model,context_pct,latency,served_model,cwd
SLACK_REACTION_TRIGGERS=true      # a reaction on the bot's own message becomes an agent turn
```

| Variable | Hermes setting | Notes |
|---|---|---|
| `SLACK_STREAMING` | `display.platforms.slack.streaming` | Turns streaming on for Slack only, not Telegram or Discord. It needs the `assistant:write` scope and Slack's Agents & AI Apps feature. Without them, Hermes falls back to editing the message as it streams. Native streaming is skipped when `unfurl_links` or `unfurl_media` is set. **Known issue:** with the pinned Hermes (v2026.9.24), a natively streamed reply can appear twice, the streamed copy and then a second message with the Block Kit table and feedback buttons, and the "is working" indicator never clears. Hermes posts the final reply again whenever its text isn't an exact continuation of the stream, and doesn't close the stream. Hermes v0.22.0 fixes this. Until the template moves to it, leave this off or set it to `false`. The boot log warns while it is on. |
| `SLACK_RICH_BLOCKS` | `slack.rich_blocks` | Applies only to sending, so the app doesn't need reinstalling. A plain-text fallback is always sent. Hermes converts the agent's markdown: headers, lists, one pipe table, `---` dividers and `>` quotes render natively, and fenced code becomes a code block. The agent can't send its own Block Kit JSON or buttons; JSON in a reply shows as a code block. A reply over Slack's 50 blocks or 39,000 characters is sent as plain text. While it's on (and `slack.markdown_blocks` isn't), the entrypoint also writes `platform_hints.slack` so the agent knows tables render, because Hermes' built-in Slack hint says they don't. A `platform_hints.slack` you wrote or edited yourself is kept. |
| `SLACK_FEEDBACK_BUTTONS` | `slack.feedback_buttons` | Hermes itself only writes clicks to the gateway log (`Feedback button clicked`). The bundled `feedback-ledger` plugin also stores them; see [Feedback ledger](#feedback-ledger). They are missing on replies that exceed Slack's 50-block limit. |
| `SLACK_NATIVE_TASK_CARDS` | `slack.native_task_cards` | Cards need a thread. Explicitly turning tool progress off also turns the cards off (`HERMES_TOOL_PROGRESS_MODE=off`, or `display.tool_progress: off` in `config.yaml`); the boot log warns when that happens. |
| `SLACK_SUGGESTED_PROMPTS` | `slack.suggested_prompts` | `default`, or JSON: `[{"title":"Check my wallet","message":"What is my SBC balance?"}]` or `{"title":"Try:","prompts":[...]}`. Slack shows up to 4, in the agent DM only. |
| `SLACK_LIVE_STATUS` | `display.platforms.slack.live_status` | `full` previews tool arguments such as file paths and commands. Use `verb` in shared channels. |
| `SLACK_RUNTIME_FOOTER` | `display.platforms.slack.runtime_footer` | The footer is plain text at the end of the reply. When streaming is on, it arrives as a separate message. `on` leaves out `cwd`, so the workspace path isn't shown. |
| `SLACK_CHANNEL_PROMPTS` | `slack.channel_prompts` | JSON object of channel ID to prompt: `{"C0123456789":"Support triage: be brief, link docs."}`. |
| `SLACK_CHANNEL_SKILL_BINDINGS` | `slack.channel_skill_bindings` | JSON list: `[{"id":"C0123456789","skills":["x402-payments"]}]`. Skills load when a session starts, so run `/new` after changing them. |
| `SLACK_REACTION_TRIGGERS` | Hermes reads it directly | Use `true`, which only routes reactions on the bot's own messages. An emoji list (`white_check_mark,eyes`) routes reactions on anyone's message. A routed reaction skips the mention rules but still checks `SLACK_ALLOWED_USERS` and `SLACK_ALLOWED_CHANNELS`. |

A variable replaces any value written into `config.yaml` by hand, and removing the variable later deletes the setting rather than restoring the hand-written value. While `SLACK_RUNTIME_FOOTER` or `SLACK_LIVE_STATUS` is set, it overrides the chat commands `/footer` and `/verbose` in Slack. Invalid values are logged as warnings and leave that setting unchanged, and so does a `config.yaml` that can't be parsed; neither stops the boot. The log shows channel IDs and counts but never prompt text. The bundled `slack/manifest.json` already has every scope, event and interactivity setting these features need (`assistant:write`, `reactions:read`, `reaction_added`/`reaction_removed`, agent view), so no reinstall is needed if your app was created from it.

### Opening the bot to a whole workspace

The agent has shell and file tools (it runs as uid 10001, not root), and `/data/.hermes/.env` holds the wallet key and API keys. Before allowing everyone (`SLACK_ALLOW_ALL_USERS=true`), turn those tools off. The wallet, x402, A2A, and ERC-8004 tools are plugins and keep working:

```
HERMES_DISABLED_TOOLSETS=terminal,file,code_execution,delegation,cronjob
HERMES_BACKGROUND_REVIEW=false        # no automatic memory writes about every user
HERMES_MEMORY_NOTIFICATIONS=off       # no "💾 Memory updated" lines in channels
```

These are written into `config.yaml` on every boot, so Railway Variables stay the source of truth. Also give the service its own LLM API key with a spend limit.

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

## Egress proxy with secret injection

Optional. Keeps provider keys and bot tokens out of the agent container entirely, so they are never in Railway's agent variables, the process environment, or `${HERMES_HOME}/.env`. A prompt-injected `printenv` or `cat .env` only finds proxy tokens.

It uses [iron-proxy](https://github.com/ironsh/iron-proxy) (v0.39.0, the version Hermes pins). Hermes ships the same pattern as `hermes egress`, but only for Docker terminal sandboxes; the agent process itself still reads real keys from `.env`. This template moves the boundary to a separate service instead:

```
agent service (tokens only) ──HTTPS_PROXY──▶ egress service (real secrets, iron-proxy) ──▶ upstream
   OPENROUTER_API_KEY=sk-or-v1-hermes-egress-…     swaps the token for the real value,
   trusts the egress CA                             only on that secret's hosts
```

- Tokens are derived from a shared seed (HMAC-SHA256), so neither service sends the other anything at runtime.
- iron-proxy terminates TLS with your CA and swaps a token only on the hosts that secret belongs to. A token sent anywhere else passes through unchanged and is worthless.
- On a secret's own host, a request without its token is refused (403), so the agent can't reach it with other credentials. Exception: hosts shared by two secrets, such as Slack's bot and app tokens.
- iron-proxy runs with only the managed secrets and a few infrastructure variables in its environment. It refuses to dial loopback, link-local (cloud metadata), RFC1918, CGNAT or IPv6 ULA addresses, which covers Railway's private network, so the proxy can't be used to reach sibling services.
- If a managed secret still has a real value on the agent service, the agent refuses to boot (`EGRESS_ENFORCE=false` downgrades that to a warning for migration).

### Set it up on Railway

1. Generate the CA and seed locally: `python3 scripts/egress/iron_egress.py gen-ca`. The output is ready for Railway's raw variable editor. Keep `EGRESS_CA_KEY` off the agent service.
2. Add a second service from this repo, named `egress`. In its Settings, set **Railway Config File** to `/railway.egress.toml`. Give it no volume and no public domain.
3. On **both** services (shared variables work well): `EGRESS_SECRETS`, `EGRESS_TOKEN_SEED`, `EGRESS_CA_CERT`.
4. On **egress** only: `EGRESS_CA_KEY` and the real value of each name in `EGRESS_SECRETS`. Railway sealed variables are a good fit.
5. On the **agent** service: `EGRESS_PROXY_URL=http://egress.railway.internal:9090`, then **delete** the real values of the managed secrets.

Example: `EGRESS_SECRETS=OPENROUTER_API_KEY,SLACK_BOT_TOKEN,SLACK_APP_TOKEN`. The agent boots with tokens under those names, `HTTPS_PROXY`/`HTTP_PROXY` pointing at egress, and `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and `NODE_EXTRA_CA_CERTS` trusting the egress CA alongside the system roots. `localhost`, `*.railway.internal` and `*.radiustech.xyz` (Radius RPC and faucet, which carry no managed secret) bypass the proxy; add more with `EGRESS_NO_PROXY`.

Locally: put the shared settings in `.env` and the real secrets plus `EGRESS_CA_KEY` in `.env.egress`, then `docker compose -f docker-compose.yml -f docker-compose.egress.yml up`.

### Which secrets can be injected

| Secret | Hosts | Where the token is swapped |
|---|---|---|
| `OPENROUTER_API_KEY` | `openrouter.ai`, `*.openrouter.ai` | `Authorization` |
| `OPENAI_API_KEY`, `VOICE_TOOLS_OPENAI_KEY` | `api.openai.com` | `Authorization` |
| `ANTHROPIC_API_KEY` | `api.anthropic.com` | `x-api-key`, `Authorization` |
| `NOUS_API_KEY` | `inference.nousresearch.com` | `Authorization` |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | `slack.com`, `*.slack.com` | `Authorization` |
| `TELEGRAM_BOT_TOKEN` | `api.telegram.org` | URL path |
| `GITHUB_TOKEN` | `api.github.com`, `uploads.github.com` | `Authorization` |
| `LINEAR_API_KEY` | `api.linear.app` | `Authorization` |
| `FIRECRAWL_API_KEY` | `api.firecrawl.dev` | `Authorization` |
| `FAL_KEY` | `fal.run`, `*.fal.run`, `*.fal.ai` | `Authorization` |
| `ELEVENLABS_API_KEY` | `api.elevenlabs.io` | `xi-api-key` |
| `BROWSERBASE_API_KEY` | `*.browserbase.com` | `X-BB-API-Key`, query string |
| `GODADDY_API_KEY`, `GODADDY_API_SECRET` | `api.godaddy.com`, `api.ote-godaddy.com` | `Authorization` |

Tokens keep each provider's key prefix (`xoxb-`, `xapp-1-`, `sk-or-v1-`, …) so client-side format checks still pass. Point a secret at other hosts with `EGRESS_HOSTS_<NAME>=host1,host2` (both services), for example `EGRESS_HOSTS_OPENAI_API_KEY` when `OPENAI_BASE_URL` is a different provider. Add secrets that aren't built in with `EGRESS_CUSTOM_SECRETS` (both services):

```json
{"ACME_API_KEY": {"hosts": ["api.acme.com"], "headers": ["X-Acme-Key"], "query": false, "path": false, "prefix": ""}}
```

These stay on the agent service, because they aren't sent as a swappable HTTP credential: `DISCORD_BOT_TOKEN` (sent inside the gateway websocket), `RADIUS_PRIVATE_KEY` (local signing; it lives in the radius-cli keystore and is no longer written to `.env`), and the inbound auth secrets `WEBHOOK_SECRET`, `JWT_API_KEY` and `HERMES_API_KEY`. `git` over HTTPS sends `GITHUB_TOKEN` base64-encoded inside Basic auth, which can't be swapped; the API and `gh` work.

### Egress allowlist

By default every host is reachable through the proxy (`EGRESS_ALLOWED_HOSTS=*`); secrets stay bound to their own hosts either way. To make the proxy a default-deny firewall, list the hosts the agent may reach on the egress service (each managed secret's hosts are added automatically, plus `*.slack-edge.com` for Slack Socket Mode). Wildcards such as `*.radiustech.xyz` work. Set `EGRESS_ALLOWLIST_WARN_ONLY=true` first to log what would be blocked without blocking it. iron-proxy logs one JSON line per request to the egress service's logs, including which secrets were swapped.

Railway can't force traffic through the proxy at the network level. A client that ignores `HTTPS_PROXY` connects directly, holding only tokens, so nothing real leaks. The x402 plugin connects directly by design, and radius-cli does too because Node's `fetch` ignores `HTTPS_PROXY`; neither uses a managed secret.

## Radius wallet

This template includes a built-in Radius Testnet wallet. On first boot, the entrypoint:

1. Instantiates a local radius-cli keystore (or reuses an existing one) under `${RADIUS_HOME:-/data/.hermes/.radius-cli}`.
2. Caches the wallet address for runtime metadata and homepage display.
3. Exports key material for JWT + ERC-8004 signing compatibility.
4. Requests SBC testnet tokens from the Radius faucet (unless `RADIUS_AUTO_FUND=false`).

The agent can then check balances, send SBC tokens, and show explorer links — all via natural language in chat.

Use the built-in wallet tools (`radius_wallet_address`, `radius_balance`, `radius_send_sbc`, `radius_tx_status`) for wallet actions. These tools are backed by `radius-cli` and use a local persistent keystore under `${RADIUS_HOME:-/data/.hermes/.radius-cli}`.

- Wallet creation happens automatically on first boot via `radius-cli wallet address`.
- The same local wallet is used for wallet actions, DID/JWT auth, homepage wallet summary, and ERC-8004 identity.
- Para-backed wallet provider support is removed in this template revision.

## x402 payments

The bundled `x402` plugin supports bounded Radius Testnet SBC payments through the installed `radius-cli` 0.5.0 `wallet pay` command. Base and mainnet paid execution are deferred. No private key is exported by this plugin.

Before using it, configure exact trusted seller hostnames in `X402_ALLOWED_HOSTS` (for example `grug402.dev`) and optionally lower `X402_MAX_PER_CALL` (default `0.01` SBC). An empty host list disables all requests, including catalog lookup and quotes. Requests require HTTPS on port 443 and public DNS addresses. Both discovery requests and the payment subprocess connect to a checked public IP while retaining hostname TLS verification; redirects cannot change the destination. Seller responses remain untrusted.

Use `x402_catalog` to inspect demo metadata, then `x402_request` with `dry_run: true` to quote an endpoint. Obtain operator approval for the destination and amount before paying. `max_amount` can only lower the configured per-call cap. The CLI validates its own challenge and uses a threshold no greater than the quoted price. The plugin never authorizes an unlimited Permit2 approval; an approval requirement must be handled separately by the operator.

`x402_ledger` reads persistent attempts from `${HERMES_HOME}/x402/ledger.jsonl`. A durable intent is written before network activity and before signing; unavailable storage blocks payment. Final write errors are returned explicitly. Protected content and URL query strings are excluded from the ledger. Up to 32 offers from each 402 response's `accepts` list (network, asset and symbol, amount, scheme, `pay_to` per offer, read from the v2 `PAYMENT-REQUIRED` header or the v1/v2 JSON body) are stored as bounded scalar values in an additive `accepts` field next to the Radius rail that was quoted or paid, so you can report on which chains and tokens sellers offer; `x402_ledger` returns an `accepts_summary` with per-network and per-token request counts. Token counts use the network and asset identifier; seller-provided symbols are display metadata. Observed offers do not enable payments on those networks. Capture is best-effort (`null` if unparseable, absent on older rows) and never affects a payment. Requester labels are caller-supplied annotations, not authenticated attribution. This local ledger is not tamper-proof.

A successful HTTP response describes delivery. `settlement_reported` requires a matching CLI payment report and transaction hash; on-chain reconciliation remains unverified. `payment_unknown` or a pending ledger entry may represent a charge: inspect transaction status before retrying. `x402_benchmark` runs at most 20 sequential requests through the same controls, reserves a fixed per-call share of its maximum 0.05 SBC run budget, and stops on unknown outcomes. There is no cumulative wallet spending limit. Use a dedicated testnet wallet and restrict who can invoke payment tools; opening a workspace bot also opens its enabled wallet tools.

Try: *"Show me three Radius Testnet x402 demo APIs"*, *"Quote this approved endpoint without paying"*, *"Show the x402 ledger"*.

## Feedback ledger

The bundled `feedback-ledger` plugin stores each 👍/👎 click on an agent reply in Slack (`SLACK_FEEDBACK_BUTTONS=true`) in `${HERMES_HOME}/feedback/ledger.jsonl`, and the agent reads it back with the `feedback_ledger` tool. Hermes still acks the click and logs `Feedback button clicked` as before; capture is added alongside it.

**How a click is captured.** At v2026.9.24 Hermes registers its own `hermes_feedback` action listener before any plugin listener, and slack_bolt runs only the first matching listener. So the plugin uses the public `register_platform_handler("slack", ...)` plugin API to add a slack_bolt global middleware. Global middleware sees every request before listeners run. The middleware checks for a `feedback_buttons` click, hands the payload to a worker thread and always passes the request on. Parsing, redaction, the session lookup and the disk write run in that thread, so they can't delay Slack's 3-second ack. If anything fails, the click is still acked, an `ERROR [feedback-ledger] could not record feedback click` line is logged, and the tool's `capture.write_failures` counter goes up. Capture never stops the gateway.

**Record.** One JSON object per click, appended under an exclusive lock and fsynced, using the same pattern as the x402 ledger. Fields can be added in later versions; readers should ignore fields they don't know.

| Field | Meaning |
|---|---|
| `schema_version`, `source` | `1`, `"slack"` |
| `event_id` | Hash of workspace, channel, message ts, user, Slack `action_ts` and value. Used as the dedupe key. |
| `ts_recorded`, `action_ts` | When the plugin saw the click (ISO-8601 UTC) and Slack's click timestamp. |
| `team_id`, `user_team_id` | Slack workspace, plus the clicker's own workspace when it differs (Slack Connect). |
| `user`, `user_name` | Slack user ID and the Slack username from the payload, both as Slack reported them. |
| `channel`, `thread_ts` | Where the rated reply is. |
| `message_ts`, `message_timestamp` | The rated reply's Slack ts, and the same time in ISO-8601 UTC. |
| `message` | First `FEEDBACK_LEDGER_EXCERPT_CHARS` characters (default 500) of the rated reply. Secret-looking strings are redacted first, using Hermes' redactor plus token, JWT, PEM and private-key patterns; transaction hashes are kept. `null` when the excerpt length is 0. |
| `message_sha256`, `message_length`, `message_truncated` | Hash of the redacted reply text, the full reply's length, and whether the excerpt is cut. |
| `rating` | `good` or `bad`. |
| `session_id`, `session_start_timestamp`, `session_match` | The Hermes session that produced the reply and when it started (ISO-8601 UTC; across a compression continuation, the start of the first session). See below. |

**Sessions.** The click payload has no session ID. The plugin reads `${HERMES_HOME}/state.db` read-only and looks for Hermes' Slack sessions for that channel, and for that thread if the reply is in one, that were live when the reply was posted. If one of those sessions has an assistant message stored within two minutes of the reply's ts, the closest one wins (`session_match: "reply_time"`). Otherwise, if every candidate belongs to one user, the newest session is used (`"thread"` or `"channel"`). Several people's sessions in one thread with no timing match give `"ambiguous"`. No session gives `"not_found"`, and an unreadable database gives `"unavailable"`. In those three cases both session fields are `null`. `reply_time` is a heuristic: it picks the closest stored assistant message. A streamed turn that runs longer than two minutes can fall outside the window and end up `ambiguous`.

**Duplicates, changes of mind and retention.** A Slack redelivery of the same click has the same `event_id`. It is skipped in memory, and the reader skips it again after a restart. Clicking 👍 and then 👎 stores two events. Summaries count only the latest click per user and reply, ordered by Slack's click time, and report it under `changed_mind`. Un-selecting a button isn't recorded as a retraction. When the file would pass `FEEDBACK_LEDGER_MAX_BYTES` (default 5 MB), it is renamed to `ledger.jsonl.1` and a new file is started. Only that one previous file is kept, so older feedback is deleted. `summary.oldest_retained` shows how far back the data goes. A click still being written during a redeploy can be lost.

**The tool.** `feedback_ledger({since, limit, rating, user, channel, session_id, message_ts, raw, include_text})`. `since` is an ISO-8601 time or an age such as `24h` or `7d`.

- It always returns `summary`: good and bad counts from each user's latest rating, `changed_mind`, raters, rated replies, a per-day trend over the last 31 days with data, and `oldest_retained`.
- Entries are the latest rating per user and reply, newest first. `raw: true` returns every click event instead.
- Reply excerpts are left out unless `include_text: true`.
- What a caller can see depends on where the tool runs (`caller_scope`):
  - Local operator surfaces (CLI, TUI, desktop) see everything.
  - A Slack session sees excerpts only from replies rated in its own channel; others come back as `message_withheld`. That way a DM reply isn't quoted into a shared channel.
  - Every other context (A2A and API-server callers, cron, other chat platforms, or no session context) gets counts, ratings and timestamps, with no excerpts and no user IDs or names.
- Set `FEEDBACK_LEDGER_TEXT_SCOPE=all` to lift these limits. They are a convenience filter, not access control: an agent with file or terminal tools can read the ledger file directly.
- Excerpts and usernames are untrusted data. A reply can quote user or web content, so the output is marked with a `notice` and `untrusted_fields`.
- `capture` reports this process's counters (`clicks_seen`, `recorded`, `duplicates`, `write_failures`, `last_write_error`). After a deploy where you've clicked a button, `clicks_seen: 0` means capture isn't wired.

**Variables.**

```
FEEDBACK_LEDGER=true                  # default on; false stops storing clicks (the tool can still read old rows)
FEEDBACK_LEDGER_EXCERPT_CHARS=500     # 0-2000; 0 stores only hash and length
FEEDBACK_LEDGER_MAX_BYTES=5000000     # 65536-20000000; rotate past this size; one previous file is kept
FEEDBACK_LEDGER_TEXT_SCOPE=channel    # channel | all
FEEDBACK_LEDGER_PATH=                 # default ${HERMES_HOME}/feedback/ledger.jsonl
```

Capture is on by default. The buttons are already shown in production, and people who click them expect the feedback to count. `state.db` on the same volume already holds full transcripts, so the ledger only adds a bounded, redacted excerpt plus Slack user IDs. Turn it off with `FEEDBACK_LEDGER=false`, or keep ratings without text with `FEEDBACK_LEDGER_EXCERPT_CHARS=0`.

**Reading it by hand on Railway.**

```bash
railway ssh --service <service>       # then, inside the container:
tail -n 20 /data/.hermes/feedback/ledger.jsonl
jq -r '[.message_timestamp, .user, .rating, .session_match] | @tsv' /data/.hermes/feedback/ledger.jsonl
```

**Limits.** The ledger is local, best-effort and not tamper-proof. Ratings are what Slack reported for the clicking user. Capture depends on Hermes' `hermes_feedback` action ID and `positive`/`negative` values and on `register_platform_handler`. CI's image smoke check (`scripts/smoke_image.py`) fails if those change when Hermes is upgraded. `tests/test_feedback_ledger.py` also has contract tests against the real Slack adapter, but they run only where Hermes is importable (not in CI's test job). If Hermes drops the plugin API, the plugin logs a warning and the tool still works. If it renames the action, `capture.clicks_seen` stays at 0.

Try: *"Show recent feedback"*, *"What are people unhappy about this week? Include the reply text"*, *"Show the feedback ledger"*.

## Paid A2A (x402 seller)

`POST /a2a` can charge callers per request in SBC on Radius Testnet using x402 v2. It is off by default; with `A2A_X402_ENABLED` unset, `/a2a` behaves exactly as before (Bearer JWT from `TRUSTED_DIDS`, plus KYA per `A2A_AUTH_MODE`).

```
A2A_X402_ENABLED=true
A2A_PRICE_SBC=0.001                 # per request; default 0.001, at most 1, six decimals
A2A_X402_FACILITATOR_URL=           # default https://facilitator.testnet.radiustech.xyz
```

A caller that passes the configured DID/KYA authentication is never charged. Enabling paid A2A also admits public callers through payment alone: the paid fallback does not require DID or KYA authentication, including when `A2A_AUTH_MODE=did_and_kya` or `kya_only` requires KYA on the authenticated path. Keep paid A2A disabled when every caller must satisfy that identity policy.

Callers that fail authentication get `402 Payment Required` with a `PAYMENT-REQUIRED` header (also returned as the JSON body): one `exact` offer for SBC on Radius Testnet (`eip155:72344`) by EIP-3009. `payTo` is this agent's wallet address (`RADIUS_WALLET_ADDRESS` or the cached radius-cli address; the keystore is never read). The paid retry carries `PAYMENT-SIGNATURE`. The server checks it against the offer and asks the facilitator to verify and settle it before invoking Hermes. Rejected payment or an unknown settlement outcome prevents backend execution. A rejected settlement returns a fresh 402; a facilitator error or unknown settlement returns `502`. Check the payer's transactions before retrying an unknown outcome.

Payment buys acceptance of a request for execution. A later backend error, delegated task failure, stream failure, or delivery timeout can occur after payment; there is no automatic refund. Successful settlement reports are returned in `PAYMENT-RESPONSE`, including when subsequent backend execution fails. These are facilitator reports and require independent on-chain reconciliation. Paid conversations are scoped to the verified payer and cannot drive trusted managed A2A sessions. When enabled, `/.well-known/agent-card.json` adds a non-standard `x402` object with the price and offer.

Paid callers reach the same configured Hermes backend and its enabled tools, including wallet plugins and any terminal/file access. Use a dedicated testnet deployment with tools and plugins restricted to the intended service, a dedicated testnet wallet, and a separate LLM API key with a spend limit. Configure those restrictions before enabling public paid access.

Paying from another agent or a terminal with radius-cli:

```bash
radius-cli wallet pay post https://<agent>/a2a \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","messageId":"m1","parts":[{"kind":"text","text":"hello"}]}}}' \
  --protocol x402 --networks radius --threshold 0.01
```

Another agent built from this template can use `x402_request` with the agent host in its `X402_ALLOWED_HOSTS`.

This is a testnet demo. The seller has no cumulative cap, rate limit, refund path, or delivery ledger. Its replay guard is per process. The facilitator checks signatures, balances, and settlement; retain its receipts and reconcile unknown outcomes before retrying.

## Slack app from code (Slack CLI)

`slack/manifest.json` is the Slack app definition (Socket Mode, agent view, the scopes and events Hermes needs). It was generated with `hermes slack manifest --agent-view`, with the generic slash commands removed so they don't collide with other apps in a shared workspace. Create or update the app with the [Slack CLI](https://docs.slack.dev/tools/slack-cli/):

```bash
brew install --cask slack-cli
slack login
cd slack && slack install        # creates the app from manifest.json and installs it
```

Then in https://api.slack.com/apps → your app:

1. **Basic Information → App-Level Tokens**: generate one with `connections:write` → `SLACK_APP_TOKEN` (`xapp-…`).
2. **OAuth & Permissions**: copy the Bot User OAuth Token → `SLACK_BOT_TOKEN` (`xoxb-…`).

Set both on the Railway service, deploy, and `/invite @Radius Agent` in the channel. Re-run `slack install` after editing the manifest.

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
| `RADIUS_HOME` | Optional radius-cli home directory. Default: `/data/.hermes/.radius-cli`. |
| `RADIUS_WALLET_ADDRESS` | Optional explicit wallet address override. Normally auto-derived from radius-cli keystore. |
| `RADIUS_NETWORK` | Wallet network. Default: `testnet`. |
| `RADIUS_RPC_URL` | Optional RPC override, read by radius-cli. Default: the RPC for `RADIUS_NETWORK`. |
| `RADIUS_SBC_ADDRESS` | Optional SBC token address override, read by radius-cli. |
| `RADIUS_CHAIN_ID` | Optional chain ID shown in `radius_tx_status` output. Default: from `RADIUS_NETWORK` (`72344` testnet, `723487` mainnet). |
| `RADIUS_EXPLORER_URL` | Optional explorer used for tx links. Default: from `RADIUS_NETWORK`. |
| `RADIUS_CLI_BIN` | Optional radius-cli binary path override. |
| `RADIUS_AUTO_FUND` | Set to `false` to skip faucet on boot. Default: enabled. |
| `RADIUS_FAUCET_URL` | Optional faucet API base for the first-boot drip. Default: `https://testnet.radiustech.xyz/api/v1/faucet`. |

The radius-cli keystore is stored under `${RADIUS_HOME}` with restricted permissions and persists across redeploys via the Railway volume.

### Wallet commands (via chat)

Once deployed, you can ask the agent:

- *"What is my wallet address?"*
- *"Check my balance"*
- *"Send 10 SBC to 0x..."*
- *"Get testnet tokens"*
- *"What is my wallet address?"*
- *"Check my Radius wallet balance now."*

The preferred interface is the bundled `radius-cli` plugin tools. Boot-time wallet setup and the first-boot faucet drip are done in `scripts/entrypoint.sh` with `radius-cli` (`wallet address`, `wallet export`, `wallet sign`); the image pins radius-cli with the `RADIUS_CLI_VERSION` build arg.

Wallet tool behavior:

- Uses one persistent local radius-cli keystore.
- No Para provider path in this template revision.

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

The FastAPI agent server lives in [scripts/agent_server](scripts/agent_server) and owns:

- the public discovery endpoints under `/.well-known/*`
- `did:web` identity and JWT auth
- `POST /token`
- `POST /a2a`
- the public homepage at `/`
- structured agent-server logging

The implementation details now live in the local agent-server README:

- [scripts/agent_server/README.md](scripts/agent_server/README.md)

That file contains:

- endpoint inventory and auth behavior
- discovery, registration, and skill publishing rules
- A2A modes, variables, and delegated webhook setup
- observability notes
- local mock-data workflow for fast homepage/UI iteration

Inbound A2A requests require an expiring JWT from a DID listed in `TRUSTED_DIDS`, or a local token obtained through `POST /token`. An unset or empty `TRUSTED_DIDS` denies external callers. On upgrade, configure each peer DID explicitly and exchange fresh local tokens; outbound tokens from `generate_a2a_token()` cannot authorize requests back to this agent.

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

The `radius-wallet.md` skill is already included and tells the agent to use the bundled Radius wallet tools. Direct CLI commands are reserved for explicit operator debugging; legacy wallet scripts have been removed.

The `using-godaddy.md` skill is also included. It tells Hermes to route GoDaddy domain availability and suggestion requests to the configured GoDaddy MCP server, to use `godaddy_dns_set_records` for setting DNS records on a known GoDaddy-managed domain, and to route Agent Name Service registry requests to the local `godaddy-ans` plugin tools. GoDaddy ANS defaults to production; set `GODADDY_ANS_ENV=ote` only when OTE is explicitly required. For example, ANS registry searches should call `godaddy_ans_search` directly instead of inspecting plugin files, running Python scripts, installing packages, or reading GoDaddy API secrets in a terminal. ANS registration uses `godaddy_ans_prepare_registration` to inspect the Swagger-aligned payload and CSRs, then `godaddy_ans_register` to submit once the agent host, endpoint URLs, and domain-validation prerequisites are correct.

Radius-maintained marketplace skills are seeded from `https://github.com/radiustechsystems/tools` at image build time, then managed at runtime as one persistent Hermes external directory (`RADIUS_SKILLS_DIR`, default `/data/.hermes/external-skills/radius-skills`). On boot, the template scans that directory for every `SKILL.md`, derives `skills.external_dirs`, and exposes those skills to Hermes as read-only external skills without copying them into `${HERMES_HOME}/skills/`.

### Auto-updating Radius external skills

Hermes local skills remain primary at `${HERMES_HOME}/skills` and are still editable by Hermes. Radius marketplace skills stay external and read-only from Hermes' perspective via `skills.external_dirs`; if a local and external skill share the same name, local wins.

To enable webhook-driven updates for the managed Radius external directory:

1. Set:
   - `RADIUS_SKILLS_AUTO_UPDATE=true`
   - `RADIUS_SKILLS_WEBHOOK_SECRET=<shared-secret>`
   - optional: `RADIUS_SKILLS_REPO` (default `radiustechsystems/tools`), `RADIUS_SKILLS_BRANCH`, `RADIUS_SKILLS_GITHUB_TOKEN`
2. Configure a GitHub webhook on the source repo (`radiustechsystems/tools` by default; the archived `radiustechsystems/skills` no longer receives pushes):
   - URL: `https://<your-agent-domain>/webhooks/github/radius-skills`
   - Event: **Push**
   - Content type: `application/json`
   - Secret: same value as `RADIUS_SKILLS_WEBHOOK_SECRET`
3. Use internal observability endpoints:
   - `GET /internal/skills/status` (Bearer internal API key)
   - optional manual refresh: `POST /internal/skills/sync` with `{"after":"<commit-sha>"}`.

`RADIUS_SKILLS_BRANCH` defaults to `main`, and only pushes to that branch are accepted. Set it to another branch name to follow that branch instead. If it is set to empty, `*`, or `any`, the webhook accepts pushes from any branch under `refs/heads/*` and syncs the pushed branch; the tools repo has many feature branches, so avoid this outside testing.

The tools repo also holds the CLI, SDK and docs, so most pushes don't change skills. Each accepted push still triggers a sync; when the skills are unchanged it leaves them as they were.

If `RADIUS_SKILLS_REPO` changes, the next sync replaces the checkout with a fresh clone of the new repository instead of fetching from the old one.

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

Hermes loads one project context file, first match wins: `.hermes.md` / `HERMES.md`, then `AGENTS.md`, then `CLAUDE.md`, then `.cursorrules`. The deployed agent therefore reads `HERMES.md`; edit that file (and `skills/*.md`) to change its behavior.

`AGENTS.md` (and `CLAUDE.md`, which imports it) are for coding agents working on this repository. They are not shipped in the image.

At runtime, gateway sessions start in `MESSAGING_CWD` (`/data/workspace` by default), not `/app`. To make the bundled project context discoverable immediately, the entrypoint links `HERMES.md`, `.hermes.md`, `README.md`, `skills/`, `plugins/`, and `scripts/` into the workspace root on boot.

`HERMES.md` and `.hermes.md` are template-owned and are force-updated on every boot so the deployed agent always uses the repo's current instructions.

### System prompt

Upstream Hermes does not read a `HERMES_SYSTEM_PROMPT` variable. To give the agent a persistent identity and behavior context, edit `HERMES.md` in your fork (it is force-copied into the workspace on every boot), or add per-channel prompts with `SLACK_CHANNEL_PROMPTS` (see [Slack experience](#slack-experience)) or under `slack.channel_prompts` in `${HERMES_HOME}/config.yaml`:

```yaml
slack:
  channel_prompts:
    "C0123456789": |
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
5. Initializes Radius wallet via radius-cli if not already done (creates the keystore, exports the key for JWT/ERC-8004, signs and requests a faucet drip).
6. Copies all local `skills/*.md` files to `${HERMES_HOME}/skills/` (overwrites on each boot).
7. Ensures the managed Radius external directory exists on persistent storage (`RADIUS_SKILLS_DIR`), bootstraps it from `/app/vendor/radius-skills` when empty (optional), scans all upstream skill directories, writes a discovery manifest, registers the derived parent roots as Hermes `skills.external_dirs`, and optionally warns or fails if `EXPECTED_VENDORED_SKILLS` are missing.
8. Copies bundled plugins from `plugins/*` to `${HERMES_HOME}/plugins/`.
9. Enables every bundled plugin in both `toolsets` and `plugins.enabled`, and removes bundled plugins from any stale `plugins.disabled` entry so persisted Railway config cannot hide newly bundled tools.
10. Links `HERMES.md`, `.hermes.md`, `README.md`, `skills/`, `plugins/`, and `scripts/` into `${MESSAGING_CWD}` so gateway sessions see the bundled project context immediately. The two context files are force-overwritten on each boot.
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
Check deploy logs for `[radius]` and `[bootstrap]` lines. Setup retries on the next boot until it succeeds. To check the wallet by hand, SSH in and run:
```bash
radius-cli wallet address
radius-cli wallet balance
```
To re-run first-boot setup (including the faucet drip), delete `${RADIUS_HOME}/initialized` and redeploy.

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

---

> ⚠️ **Demo code — not production-ready.** Provided as-is, without warranty, and may contain known, unpatched vulnerabilities (including in dependencies). If you reuse it, run your own security and supply-chain scans and patch before deploying.
