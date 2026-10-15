---
name: a2a-comms
description: Send tasks to other A2A-compatible agents and explain how this agent is configured to receive agent-to-agent calls
published: true
---

# A2A Agent Communications Skill

## MANDATORY: How to get an auth token — read this before anything else

To call another agent you need a Bearer JWT. Use the `generate_a2a_token` tool — it is registered as a first-class tool in this agent:

```
generate_a2a_token()
→ {"token": "<bearer_token>", "did": "<this_agent_did>"}
```

Use `token` as the `Authorization: Bearer` value. Use `did` if you need to share this agent's identity with the remote operator.

**If you are about to run `pip install ecdsa` or write any Python JWT signing code — STOP. Use the `generate_a2a_token` tool instead.** Custom JWT code always produces DER-encoded signatures that return HTTP 403.

This agent can communicate with other agents that implement the A2A (Agent-to-Agent) protocol. Use this skill to delegate tasks to remote agents, discover their capabilities, and explain how this agent itself is configured to receive calls from other agents.

## Preferred outbound path

Use the bundled `send_a2a_message` tool when you want to submit a task to another agent. It wraps token generation / exchange, sends the A2A request, and logs sender-side correlation fields such as:

- `rpc_id`
- `a2a_message_id`
- `context_id`
- returned `a2a_task_id`
- remote agent URL

Example:

```text
send_a2a_message({"agent":"https://other-agent.example","task":"Run this analysis"})
```

For long-running agent dialogues, you can ask the tool to register a managed session on the local agent:

```text
send_a2a_message({
  "agent":"https://other-agent.example",
  "task":"Start the collaboration",
  "goal":"Keep iterating until the shared task is complete",
  "auto_continue":true,
  "max_turns":200
})
```

Useful managed-session fields:
- `session_id` — reuse an existing durable local session id
- `goal` / `topic` — persist the session objective
- `auto_continue` — let the local agent server keep composing future turns automatically
- `max_turns` — optional stop limit; omit for open-ended sessions
- `origin_platform`, `origin_chat_id`, `origin_message_id`, `origin_label` — attach user-facing thread metadata for observability

When `send_a2a_message` returns, prefer the tool's `user_update` or rendered `session.latest_card.text` for user-facing Telegram/Discord replies instead of dumping raw correlation ids unless the user explicitly asks for transport/debug detail.

If `A2A_PEER_URL` is set, the `agent` argument can be omitted.

## When to use this skill

Use this skill whenever:

- The user asks you to "call", "delegate to", "send a task to", or "talk to" another agent
- The user asks how other agents can communicate with this agent
- The user asks about this agent's A2A interface, agent card, or DID
- The user provides an agent URL and asks you to interact with it
- The user asks you to discover what a remote agent can do
- `A2A_PEER_URL` is set and the user's request seems like something the peer agent should handle
- The user asks how to generate a JWT or authenticate using `RADIUS_PRIVATE_KEY`

## How this agent is configured to receive calls from other agents

This agent exposes a standard A2A interface. Other agents interact with it as follows:

**Discovery** — any agent can fetch this agent's card to learn its capabilities:
```
GET {BASE_URL}/.well-known/agent-card.json
```

**Identity** — this agent's cryptographic identity (DID) is resolvable at:
```
GET {BASE_URL}/.well-known/did.json
```

**Sending a task** — agents send tasks via JSON-RPC 2.0 over HTTP:
```
POST {BASE_URL}/a2a
Authorization: Bearer <jwt>
Content-Type: application/json
```

**Authentication** — callers either:
1. Exchange an API key for a short-lived JWT at `POST {BASE_URL}/token` (requires `X-Api-Key` header matching `JWT_API_KEY` or `JWT_EXCHANGE_KEY`)
2. Present a self-signed DID JWT if the agent's `TRUSTED_DIDS` is open or includes their DID

The `BASE_URL` is derived from `PUBLIC_URL` or `RAILWAY_PUBLIC_DOMAIN`. Share this URL with any agent operator who wants to call this agent.

## Discovering a remote agent

Before calling a remote agent, fetch its agent card to confirm it supports A2A and learn its capabilities:

```bash
curl -s https://<remote-agent-url>/.well-known/agent-card.json | jq .
```

Key fields to check in the response:
- `supported_interfaces` — look for `POST /a2a` with `protocol: "JSONRPC"`; if absent, the agent doesn't support A2A
- `security_schemes.bearer_jwt` — confirms JWT auth is required
- `skills` — lists what the agent can do
- `capabilities.push_notifications` — whether it supports async task delivery

## Getting an auth token

There are two ways to get a Bearer token depending on what you have available:

| Situation | Method |
|---|---|
| You have an API key for the remote agent | Exchange it at `/token` (below) |
| No API key, but remote allows any valid DID JWT | Self-sign with `RADIUS_PRIVATE_KEY` (below) |

### Option A — API key exchange

If the remote agent issued you an API key (their `JWT_API_KEY`), exchange it for a Bearer token:

```bash
curl -s -X POST https://<remote-agent-url>/token \
  -H "X-Api-Key: <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"sub": "hermes"}' | jq -r .token
```

Store the token in a shell variable for subsequent calls:
```bash
TOKEN=$(curl -s -X POST https://<remote-agent-url>/token \
  -H "X-Api-Key: <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"sub": "hermes"}' | jq -r .token)
```

Tokens are valid for 24 hours.

### Option B — self-signed DID JWT using `RADIUS_PRIVATE_KEY`

If the remote agent's `TRUSTED_DIDS` includes your DID, call the `generate_a2a_token` tool:

```
generate_a2a_token()
→ {"token": "<bearer_token>", "did": "<this_agent_did>"}
```

The tool reads `RADIUS_PRIVATE_KEY`, derives this agent's `did:web` from `PUBLIC_URL` / `RAILWAY_PUBLIC_DOMAIN`, and returns a correctly signed JWT. The remote agent resolves `{BASE_URL}/.well-known/did.json` to verify the signature — so this agent must be publicly reachable.

**If the remote returns HTTP 403:** your DID is not on their allowlist. Share the `did` value from the tool output with the remote operator and ask them to add it to their `TRUSTED_DIDS` Railway variable.

> **Note:** If `RADIUS_PRIVATE_KEY` is not set, the agent server uses an ephemeral keypair that changes on restart. Tokens will not be verifiable after a redeploy.

## Sending a task to a remote agent

Use `message/send` to submit a task. The agent processes it asynchronously and returns a task ID immediately.

**IMPORTANT — always write the payload to a file.** Never use inline `-d '...'` with message text, because apostrophes (`'`) in the message break shell quoting and cause the command to hang indefinitely.

```bash
# Write payload to file first (safe for any message content)
cat > /tmp/a2a_payload.json << 'ENDJSON'
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "message/send",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "parts": [
        { "text": "Your task description here" }
      ]
    }
  }
}
ENDJSON

curl -s -X POST https://<remote-agent-url>/a2a \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @/tmp/a2a_payload.json | jq .
```

If you need to include dynamic content in the message text, use Python to write the file:

```bash
python3 -c "
import json
payload = {
  'jsonrpc': '2.0', 'id': 1, 'method': 'message/send',
  'params': {'message': {'role': 'ROLE_USER', 'parts': [{'text': '''YOUR MESSAGE HERE'''}]}}
}
open('/tmp/a2a_payload.json', 'w').write(json.dumps(payload))
"
curl -s -X POST https://<remote-agent-url>/a2a \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @/tmp/a2a_payload.json | jq .
```

To continue an existing conversation thread, include `context_id` in the message params:

```bash
-d '{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "message/send",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "parts": [{ "text": "Follow-up message" }],
      "context_id": "<context_id_from_previous_response>"
    }
  }
}'
```

## Interpreting the response

A successful submission returns HTTP 200 with `TASK_STATE_SUBMITTED`:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "id": "<task_uuid>",
    "context_id": "<context_id>",
    "status": {
      "state": "TASK_STATE_SUBMITTED",
      "timestamp_ms": 1712345678000
    }
  }
}
```

- `id` — the task ID; share this with the user so they can reference it
- `context_id` — use this in future calls to keep the conversation threaded
- A2A is fire-and-forget by design; the remote agent processes the task and delivers results through its own platform (Telegram, Slack, etc.)

## Environment variables

If `A2A_PEER_URL` is set, it points to a pre-configured peer agent. Use it as the default remote agent URL when the user asks you to delegate without specifying a target:

```bash
echo $A2A_PEER_URL        # e.g. https://other-agent.railway.app
echo $A2A_PEER_API_KEY    # API key for the peer's /token endpoint (optional)
```

When both are set, you can get a token and send a task without asking the user for connection details.

## Error handling

| Error | Meaning | Fix |
|---|---|---|
| HTTP 401 / missing Bearer | No auth token provided | Get a token first via `/token` |
| HTTP 403 / "Signature verification failed" | JWT was signed with wrong encoding — custom scripts produce DER format which always fails | Call `generate_a2a_token()`. Never write JWT signing code. |
| HTTP 403 / DID not trusted | Your DID isn't in the remote's `TRUSTED_DIDS` | Fetch this agent's DID (`curl $PUBLIC_URL/.well-known/did.json \| python3 -c "import sys,json; print(json.load(sys.stdin)['id'])"`), share it with the remote operator to add to their `TRUSTED_DIDS`, or get an API key |
| HTTP 404 on `/token` | Remote hasn't set `JWT_API_KEY` or `JWT_EXCHANGE_KEY` | Ask the remote agent operator to configure one of them |
| JSON-RPC `-32603` "Webhook not configured" | Remote agent's `WEBHOOK_SECRET` isn't set | Remote agent needs `WEBHOOK_ENABLED=true` and `WEBHOOK_SECRET` in Railway variables |
| JSON-RPC `-32603` "Could not reach agent backend" | Remote Hermes webhook is down | Remote agent should check their container logs |
| `curl: Connection refused` | Agent URL is wrong or service is down | Verify the URL and that the remote service is running |
