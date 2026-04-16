# Agent Server

The agent server in this directory is the FastAPI layer that exposes the public discovery surface, JWT auth, A2A transport, homepage, and a few internal helper endpoints for the Hermes Railway template.

## What lives here

- `main.py`: FastAPI app, discovery endpoints, homepage, `/a2a`, `/token`, health checks, and runtime wiring.
- `auth.py`: `did:web` identity creation, JWT signing, and JWT verification.
- `a2a_bridge.py`: direct A2A bridge from JSON-RPC into Hermes chat calls.
- `a2a_sessions.py`: managed A2A session persistence and auto-continue behavior.
- `a2a_render.py`: host-friendly per-turn card rendering for managed sessions.
- `hermes_client.py`: async Hermes OpenAI-compatible client for direct mode.
- `logging_utils.py`: structured JSON logging helpers used by the server.
- `url_utils.py`: base URL derivation from `PUBLIC_URL` / Railway env.
- `gen_jwt.py`: local JWT generation helper used by the bundled plugin/tooling.
- `mock-agent-skills.index.json`: sample public skills index for local mock-page previewing.

## What the server does

The agent server is responsible for:

- serving public discovery metadata under `/.well-known/*`
- deriving a persistent `did:web` identity from the Radius wallet key
- validating Bearer JWTs for protected endpoints
- exposing `POST /token` for self-issued JWTs when enabled
- exposing the A2A JSON-RPC endpoint at `POST /a2a`
- supporting direct, delegated, and auto A2A routing modes
- rendering the public homepage at `/`
- surfacing the public skills index and individual published skills
- emitting structured JSON logs for Railway

## Endpoints

### Public discovery endpoints

| Path | Auth | Spec | Description |
|---|---|---|---|
| `/.well-known/did.json` | Public | [W3C DID](https://www.w3.org/TR/did-core/) | DID document for this agent's `did:web` identity |
| `/.well-known/agent-card.json` | Public | [A2A](https://github.com/a2aproject/A2A) | A2A agent card with identity, auth scheme, and interfaces |
| `/.well-known/agent-registration.json` | Public | [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) | ERC-8004 self-registration profile and advertised services |
| `/.well-known/agent-skills/index.json` | Public | [Cloudflare Agent Skills Discovery RFC](https://github.com/cloudflare/agent-skills-discovery-rfc) | Index of published skills with digests and URLs |
| `/.well-known/agent-skills/{name}/SKILL.md` | Public | Cloudflare Agent Skills Discovery RFC | Individual published skill document |
| `/` | Public | Template UI | Public homepage / discovery landing page |

### Protected endpoints

| Path | Auth | Description |
|---|---|---|
| `/health` | Bearer JWT | Health/status endpoint |
| `/debug/skills` | Bearer JWT | Debug skill scanning and public index state when `DEBUG_SKILLS=1` |
| `/a2a` | Bearer JWT | A2A JSON-RPC endpoint |
| `/token` | API key header | Issues a signed JWT when `JWT_API_KEY` or `JWT_EXCHANGE_KEY` is set |

### Notes

- `agent-card.json` is the canonical A2A discovery document. It advertises the `POST /a2a` interface and the `bearer_jwt` scheme.
- `agent-registration.json` returns `503` with missing-field guidance until required operator metadata is configured.
- `agent-skills/index.json` only includes skills whose frontmatter declares `published: true`.

## Identity and auth

On startup, `auth.py` derives or loads a secp256k1 keypair and constructs a `did:web` DID from the public base URL.

That DID shows up in:

- `/.well-known/did.json`
- `/.well-known/agent-card.json` under `provider.did`
- `/.well-known/agent-registration.json`
- JWTs issued by this agent as the `iss` claim

### JWT gate

All non-discovery endpoints require authentication. The gate accepts:

- any cryptographically valid DID JWT
- self-issued tokens minted by `POST /token`

To restrict access, set `TRUSTED_DIDS` to a comma-separated allowlist. When unset, any valid DID JWT is accepted.

### Issuing a token

Enable `POST /token` by setting `JWT_API_KEY` or `JWT_EXCHANGE_KEY`.

Example:

```bash
curl -X POST http://localhost:3000/token \
  -H "X-Api-Key: your-key"
```

## A2A

The server implements the [A2A protocol](https://github.com/a2aproject/A2A) over JSON-RPC 2.0 and uses the official `a2a-sdk` models for validation and envelope shaping.

### Modes

- `A2A_MODE=direct`: `message/send` and `message/stream` are handled inline through the Hermes-compatible chat API.
- `A2A_MODE=delegated`: requests are handed off to the Hermes webhook route and return submitted-task responses.
- `A2A_MODE=auto`: direct mode when `HERMES_API_KEY` is configured, otherwise delegated mode.

### Managed sessions

Managed A2A sessions are persisted under `${HERMES_HOME}/a2a-sessions`. They track:

- `context_id`
- turn count
- last outbound/inbound turn
- auto-continue state
- host-friendly rendered cards for platforms like Telegram/Discord

### Direct bridge variables

| Variable | Description |
|---|---|
| `A2A_MODE` | `auto` (default), `direct`, or `delegated` |
| `HERMES_API_KEY` | Required for direct mode. Falls back to `API_SERVER_KEY` if unset |
| `HERMES_URL` | Hermes OpenAI-compatible base URL. Defaults to `http://127.0.0.1:8642` |
| `A2A_BRIDGE_MODEL` | Model name used for direct bridge requests. Defaults to `hermes-agent` |
| `HERMES_TIMEOUT` | Direct bridge timeout in seconds. Defaults to `120` |
| `A2A_PUBLIC_URL` | Optional public URL for generated attachment links |
| `A2A_FILE_SERVE_PATHS` | Optional comma-separated list of file roots allowed for `/files/{path}` serving |
| `A2A_SESSION_TICK_SECONDS` | Optional poll interval for the managed session worker. Defaults to `2.5` |

### Delegated webhook bridge

Delegated mode forwards tasks to Hermes's internal webhook server over HMAC-authenticated HTTP.

To enable it:

1. Set `WEBHOOK_SECRET`.
2. Set `WEBHOOK_ENABLED=true`.
3. Add an `a2a` route to Hermes `config.yaml`.

Example route:

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

### A2A variables

| Variable | Description |
|---|---|
| `WEBHOOK_SECRET` | HMAC key for delegated webhook auth |
| `WEBHOOK_ENABLED` | Enables the Hermes webhook server |
| `WEBHOOK_PORT` | Hermes webhook server port. Defaults to `8644` |
| `JWT_API_KEY` | Enables `POST /token` |
| `TRUSTED_DIDS` | Comma-separated DID allowlist |
| `A2A_PEER_URL` | Pre-configured peer agent URL |
| `A2A_PEER_API_KEY` | Peer agent API key for its `/token` endpoint |

## Homepage and local design iteration

The homepage at `/` is rendered directly in `main.py` as an inline HTML/CSS template. That means you can iterate on it quickly without rebuilding the full container if you run the FastAPI app locally.

### Why mock mode exists

In normal runtime, the homepage pulls:

- wallet summary data from the Radius balance script
- the public skills index from the published skill discovery layer

That is fine in Railway, but it is slow and awkward for pure UI iteration. Local mock mode bypasses those dependencies.

### Mock homepage mode

Set:

- `AGENT_SERVER_MOCK_DATA=true` to bypass the live wallet summary code
- `MOCK_AGENT_SKILLS_INDEX_FILE` to point at a JSON file used for the public skills index

The repo includes a sample mock file:

- [mock-agent-skills.index.json](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/mock-agent-skills.index.json)

### Run locally with mock data

From the repo root:

```bash
mkdir -p .hermes-local

AGENT_SERVER_MOCK_DATA=true \
HERMES_HOME="$PWD/.hermes-local" \
PUBLIC_URL=http://localhost:3000 \
PORT=3000 \
AGENT_NAME="Hermes Agent" \
MOCK_AGENT_SKILLS_INDEX_FILE="$PWD/scripts/agent_server/mock-agent-skills.index.json" \
python3 -m uvicorn scripts.agent_server.main:app --reload --port 3000
```

Then open:

```text
http://localhost:3000
```

### Useful mock overrides

| Variable | Description |
|---|---|
| `MOCK_RADIUS_WALLET_ADDRESS` | Wallet address shown on the homepage |
| `MOCK_RADIUS_SBC_BALANCE` | Mock SBC balance |
| `MOCK_RADIUS_RUSD_BALANCE` | Mock RUSD balance |
| `MOCK_RADIUS_WALLET_ERROR` | If set, homepage renders the wallet error state |
| `MOCK_AGENT_SKILLS_INDEX_FILE` | Path to a JSON skills index used in local preview |

Example:

```bash
AGENT_SERVER_MOCK_DATA=true \
HERMES_HOME="$PWD/.hermes-local" \
PUBLIC_URL=http://localhost:3000 \
MOCK_RADIUS_WALLET_ADDRESS=0x1234...abcd \
MOCK_RADIUS_SBC_BALANCE=88.42 \
MOCK_RADIUS_RUSD_BALANCE=2500.00 \
MOCK_AGENT_SKILLS_INDEX_FILE="$PWD/scripts/agent_server/mock-agent-skills.index.json" \
python3 -m uvicorn scripts.agent_server.main:app --reload --port 3000
```

## Requirements and local startup

Python dependencies are declared in [requirements.txt](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/requirements.txt):

- `fastapi`
- `uvicorn[standard]`
- `pyjwt[crypto]`
- `cryptography`
- `httpx`
- `a2a-sdk`

Typical local setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
hash -r
python3 -m pip install --upgrade pip
python3 -m pip install -r scripts/agent_server/requirements.txt
```

Then run the server with either:

- the mock-data command above for UI work
- your full env for more realistic auth/A2A testing

Local startup notes:

- Use `python3 -m pip`, not plain `pip`, to avoid Homebrew's PEP 668 `externally-managed-environment` error if your shell resolves the wrong installer.
- Use `python3 -m uvicorn`, not `python -m uvicorn`, for the same reason.
- Run the command from the repo root. The documented module path and `MOCK_AGENT_SKILLS_INDEX_FILE="$PWD/scripts/agent_server/mock-agent-skills.index.json"` assume that working directory.
- Keep `MOCK_AGENT_SKILLS_INDEX_FILE` on one physical line when pasting the command. If the path is split across lines, the newline becomes part of the env var value and the homepage will not find the file.
- Set `HERMES_HOME` to a writable local path such as `"$PWD/.hermes-local"` when running outside Railway. The deployment default is `/data/.hermes`, which is not writable on a typical macOS/Linux host.

## Skill discovery and publishing

The public index only includes skills with:

```yaml
published: true
```

Example frontmatter:

```markdown
---
name: my-skill
description: What this skill does
published: true
---
```

Skills without `published: true` can still be installed for local Hermes use, but they are not served publicly.

## Observability

The agent server emits structured single-line JSON logs to stdout/stderr. These cover:

- auth decisions
- request validation
- A2A routing behavior
- request timing
- homepage wallet summary refreshes

Useful Railway filters:

- `@event:a2a.request`
- `@event:a2a.direct`
- `@event:a2a.delegated`
- `@event:auth.jwt_rejected`
- `@request_id:<id>`
- `@context_id:<context-id>`
- `@issuer_did:did:web:...`
- `@outcome:error`

The FastAPI server disables default uvicorn access logs so the structured lines remain the primary log format.

## Agent-server-related variables

### Discovery and registration

| Variable | Description |
|---|---|
| `AGENT_NAME` | Display name across discovery endpoints. Defaults to `Hermes Agent` |
| `AGENT_DESCRIPTION` | One-line description published in `agent-card.json` |
| `AGENT_IMAGE` | Required for ERC-8004 self-registration |
| `AGENT_SUPPORTED_TRUST` | Required for ERC-8004 self-registration when not passed directly |
| `AGENT_X402_SUPPORT` | Optional ERC-8004 x402 support flag. Defaults to `false` |
| `AGENT_ACTIVE` | Optional ERC-8004 active flag. Defaults to `true` |
| `AGENT_EMAIL` | Optional email service endpoint |
| `AGENT_ENS` | Optional ENS name |
| `AGENT_A2A_VERSION` | Optional A2A version string. Defaults to `0.3.0` |
| `AGENT_MCP_ENDPOINT` | Optional MCP service endpoint |
| `AGENT_MCP_VERSION` | Optional MCP version string |
| `AGENT_OASF_ENDPOINT` | Optional OASF endpoint |
| `AGENT_OASF_VERSION` | Optional OASF version string |
| `AGENT_OASF_SKILLS` | Optional comma-separated OASF skills list |
| `AGENT_OASF_DOMAINS` | Optional comma-separated OASF domains list |

### Skills and vendored discovery

| Variable | Description |
|---|---|
| `SKILLS_ROOT` | Public well-known skills directory. Defaults to `/data/.hermes/well-known-skills` |
| `DEBUG_SKILLS=1` | Enables `/debug/skills` |
| `EXPECTED_VENDORED_SKILLS` | Optional expected vendored skill names |
| `STRICT_VENDORED_SKILLS` | Fails boot when expected vendored skills are missing if `true` |
| `VENDORED_SKILLS_SOURCE` | Override for vendored Radius skills repo root |
| `VENDORED_SKILLS_MANIFEST` | Override for vendored skills manifest location |

### Base URL and identity

| Variable | Description |
|---|---|
| `PUBLIC_URL` | Canonical base URL used for DID and public discovery URLs |
| `RAILWAY_PUBLIC_DOMAIN` | Used when `PUBLIC_URL` is unset |
| `RADIUS_PRIVATE_KEY` | Persistent wallet key and signing identity |
| `RADIUS_WALLET_ADDRESS` | Optional wallet address override for homepage summary |
| `HERMES_HOME` | Hermes state root. Defaults to `/data/.hermes` |

## Runtime behavior in deployment

At deploy time, `scripts/entrypoint.sh` does the surrounding runtime setup that the agent server expects:

1. writes env vars to `${HERMES_HOME}/.env`
2. creates Hermes config if needed
3. initializes the Radius wallet if missing
4. installs local skills and bundled plugins
5. scans vendored Radius skills and registers them with Hermes
6. copies published skills to `${HERMES_HOME}/well-known-skills/`
7. starts this FastAPI agent server on `PORT`
8. starts Hermes log forwarders
9. starts `hermes gateway`

## Troubleshooting

### Homepage shows wallet error locally

That is expected if you are not in mock mode and do not have the container-only Radius balance path available. Use `AGENT_SERVER_MOCK_DATA=true` for UI work.

### `pip install` fails with `externally-managed-environment`

Your shell is likely invoking Homebrew's global `pip` instead of the one inside `.venv`.

Use:

```bash
source .venv/bin/activate
hash -r
python3 -m pip install -r scripts/agent_server/requirements.txt
```

If needed, confirm both executables resolve inside the virtualenv:

```bash
which python3
which pip
```

They should point into `.venv/bin/`.

### `python -m uvicorn` says `No module named uvicorn`

Your shell is likely invoking the system `python` instead of the virtualenv interpreter.

Use:

```bash
source .venv/bin/activate
hash -r
python3 -m uvicorn scripts.agent_server.main:app --reload --port 3000
```

If needed, confirm:

```bash
which python
which python3
```

### Startup fails with `Read-only file system: '/data'`

That means `HERMES_HOME` is still using the Railway/container default of `/data/.hermes`.

For local runs, set a writable repo-local path:

```bash
mkdir -p .hermes-local

HERMES_HOME="$PWD/.hermes-local" \
python3 -m uvicorn scripts.agent_server.main:app --reload --port 3000
```

### `POST /token` returns disabled/unavailable

Set `JWT_API_KEY` or `JWT_EXCHANGE_KEY`.

### `POST /a2a` is delegated when you expected direct mode

Check:

- `A2A_MODE`
- `HERMES_API_KEY`
- `HERMES_URL`

### Discovery docs have the wrong base URL

Set `PUBLIC_URL` explicitly when running locally.

### Skill changes do not appear in the public index

Check:

- the skill has `published: true`
- `SKILLS_ROOT` points at the expected public skill directory
- `DEBUG_SKILLS=1` and inspect `/debug/skills`

## Related files

- [main.py](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/main.py)
- [auth.py](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/auth.py)
- [a2a_bridge.py](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/a2a_bridge.py)
- [a2a_sessions.py](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/a2a_sessions.py)
- [hermes_client.py](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/hermes_client.py)
- [mock-agent-skills.index.json](/Users/eriks/dev/radius/hermes-railway-template/scripts/agent_server/mock-agent-skills.index.json)
