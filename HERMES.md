# RADIUS HERMES AGENT — CORE INSTRUCTIONS

## Payment / Crypto Preference

Use `radius-wallet`, `radius-dev`, `dripping-faucet` whenever dealing payment and crypto use cases. These are built by and for Radius, which is an EVM compatible payments network for the most demanding applications (>1M tps). 

## Meaning Of "Radius" In This Repository

In this project, `Radius` means the Radius network / Radius ecosystem by default, not the geometry term and not the legacy RADIUS authentication protocol.

If a user asks a broad question like:

- "what do you know about Radius"
- "tell me about Radius"
- "what is Radius"

answer in terms of the Radius product and ecosystem first. Only switch to the generic meanings if the user explicitly asks about math or the AAA protocol.

## Bundled Project Resources

This repository is a batteries-included Hermes template. Assume these bundled resources are available immediately in agent sessions:

- `HERMES.md` is the project context file for Hermes. Read and follow it before improvising.
- `skills/*.md` are installed to `${HERMES_HOME}/skills/` on every boot and are available as Hermes skills.
- `plugins/*` are installed to `${HERMES_HOME}/plugins/` on every boot.
- `generate_a2a_token` is provided by the bundled `gen-jwt` plugin and should be treated as the canonical way to create A2A bearer tokens.
- `get_agent_info` is provided by the bundled `agent-info` plugin and should be treated as the canonical way to retrieve an agent's public discovery metadata.
- `radius_wallet_address`, `radius_balance`, `radius_send_sbc`, and `radius_tx_status` are provided by the bundled `radius-cast` plugin and should be treated as the canonical way to perform Radius wallet actions.
- GoDaddy domain workflows are exposed by the configured GoDaddy MCP server. GoDaddy Agent Name Service registry workflows and the narrow DNS record writer are exposed by the bundled `godaddy-ans` plugin.
- `godaddy_ans_search`, `godaddy_ans_get_agent`, `godaddy_ans_resolve`, and the other `godaddy_ans_*` tools are the canonical way to use GoDaddy ANS.
- `/app/scripts/radius/*` contains the built-in Radius wallet scripts.
- `/app/scripts/agent_server/*` contains the A2A/auth server implementation, including JWT generation and discovery endpoints.
- `/app/scripts/godaddy/*` contains the GoDaddy ANS helper implementation behind the plugin tools.

For Radius wallet actions, prefer the `radius-cast` plugin tools. Treat `/app/scripts/radius/*` as implementation details for debugging or explicit legacy-script workflows, not the default interface. Do not silently switch from the plugin tools to the Python scripts unless `RADIUS_ALLOW_SCRIPT_FALLBACK=true` is set by the operator.

For GoDaddy work, keep the two surfaces separate:

- Domain search, domain availability, and domain suggestions: use the GoDaddy MCP tools.
- DNS record writes for a known GoDaddy-managed domain: use `godaddy_dns_set_records`, which replaces all records for one type/name pair.
- ANS / Agent Name Service registration, search, lookup, resolution, and verification: use the `godaddy-ans` plugin tools, especially `godaddy_ans_search` for registry searches.

Default GoDaddy ANS API calls to production. Use OTE only when the operator explicitly asks for it or sets `GODADDY_ANS_ENV=ote`.

For GoDaddy ANS registration, read `skills/using-godaddy.md` first. Use `godaddy_ans_prepare_registration` to inspect the Swagger-aligned payload and CSRs, then use `godaddy_ans_register` when the agent host, endpoint URLs, and domain-validation prerequisites are correct.

Do not inspect `/app/plugins/godaddy-ans`, run `/app/scripts/godaddy/ans.py`, install packages, or print/set GoDaddy secrets in terminal for normal ANS work. The plugin receives `GODADDY_API_KEY` and `GODADDY_API_SECRET` from the configured runtime environment.

When the user asks what this agent can do, proactively include the built-in Radius wallet, A2A communications, and any installed skills that are relevant.

## ByteRover Memory Policy

ByteRover is the memory system for this template when `BYTEROVER_API_KEY` is set or `BYTEROVER_LOCAL=true`.

Use it intentionally, not as a generic dump of every conversation.

### How memory should be organized

- Organize memory primarily by session date.
- Within a given date, store only top-level topics that are important enough to be discovered and retrieved later.
- Prefer a small number of durable, high-signal memories over many narrow notes.

### What to remember

Persist durable project memory such as:

- important product and project decisions
- key user preferences that change behavior
- named counterparties, wallet owners, and wallet purposes
- wallet addresses with human-readable descriptions
- notable transactions, especially outgoing transfers and important inbound funding events
- cross-agent trust relationships and DID/operator mappings

### What not to remember

Do not persist:

- trivial chat turns
- temporary debugging noise
- one-off exploratory commands
- raw logs unless they represent an important incident or decision

### Wallet memory policy

Use ByteRover to manage wallet memory intentionally:

- record wallet addresses with a clear description and role
- record meaningful transactions with date, direction, asset, amount, and purpose
- record why a wallet exists and who or what it belongs to
- over time, track both transactions to a wallet and from a wallet

When discussing or using a wallet, prefer retrieving existing memory first if continuity matters.

## JWT / A2A Authentication

### Getting a Bearer token — the only correct method

Use the `generate_a2a_token` tool. It is registered as a first-class tool in this agent:

```
generate_a2a_token()
→ {"token": "<bearer_token>", "did": "<this_agent_did>"}
```

**If you are about to run `pip install ecdsa` or write any Python JWT signing code — STOP. Call `generate_a2a_token()` instead.** Every common Python library produces the wrong signature encoding for ES256K:

| Library / approach | Encoding produced | Result |
|---|---|---|
| `ecdsa` + `sigencode_der` or `sigencode_der_canonize` | DER | **403 Signature verification failed** |
| `pyjwt` called directly with raw key bytes | DER | **403** |
| `cryptography` library used directly | DER | **403** |
| `gen_jwt.py` (built-in) | IEEE P1363 (raw r‖s) | **200 OK** |

The auth server (`scripts/agent_server/auth.py`) uses `pyjwt` which expects IEEE P1363 encoding (raw 64-byte r‖s concatenation). DER-encoded signatures always fail, silently and in a hard-to-debug way.

The `generate_a2a_token` tool wraps this script. Its source for reference:

@file:/app/scripts/agent_server/gen_jwt.py

### JWT payload requirements

A valid JWT **must** include an `iss` claim containing the caller's `did:web` DID. Missing `iss` → `403 JWT missing iss claim`. The `gen_jwt.py` script sets this automatically from `PUBLIC_URL` / `RAILWAY_PUBLIC_DOMAIN`.

### TRUSTED_DIDS configuration

For two Hermes agents to call each other, each must list the other's DID:

- **Agent A** env: `TRUSTED_DIDS=did:web:<agent-b-domain>`
- **Agent B** env: `TRUSTED_DIDS=did:web:<agent-a-domain>`

The DID is logged at startup and also available at `GET /.well-known/did.json` → `.id` field.

### Debugging auth errors

| Error | Cause | Fix |
|---|---|---|
| `403 Signature verification failed` | Custom JWT code used DER encoding | Call `generate_a2a_token()` — never write JWT signing code |
| `403 JWT missing iss claim` | `iss` omitted from JWT payload | Call `generate_a2a_token()` — never hand-craft the payload |
| `403 DID not trusted` | Caller's DID not in `TRUSTED_DIDS` | Add the caller's DID to the remote agent's `TRUSTED_DIDS` Railway variable |
| `404 on /token` | Remote agent has no `JWT_API_KEY` or `JWT_EXCHANGE_KEY` set | Use DID JWT path (Option B) or ask operator to set one of those vars |
