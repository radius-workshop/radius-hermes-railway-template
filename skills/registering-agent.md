---
name: registering-agent
description: Register this agent or inspect ERC-8004 registrations on Radius using the deterministic registry plugin tools
published: true
---

# Registering Agent Skill

Use this skill when the user wants to register an agent on ERC-8004, inspect a specific registration, or list all registered agents on Radius.

## Preferred interface

Use the bundled `erc8004-registry` plugin tools. Do not write temporary Web3 scripts or custom transaction code when the plugin can handle the request.

Available tools:

- `erc8004_get_registration`
- `erc8004_list_registrations`
- `erc8004_get_registry_stats`
- `erc8004_register_self`
- `erc8004_register_agent`
- `erc8004_update_agent_uri`

## Network model

The plugin exposes two checked-in network targets:

- `testnet`
- `mainnet`

Use `testnet` by default. If `mainnet` has not been enabled in the checked-in constants yet, surface that clearly instead of guessing RPC or contract values.

## Registration shape

The canonical registration shape in this repo is:

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

The plugin normalizes field order and encodes the registration as `data:application/json;base64,...`.

## Behavioral rules

1. For the common case of registering this current Hermes agent, prefer `erc8004_register_self`.
2. For `erc8004_register_self`, collect missing operator-owned profile fields such as `name`, `description`, `image`, and `supportedTrust` before calling the tool. Do not invent them.
3. The self-registration flow derives the current agent's `web`, `A2A`, and `DID` service entries automatically. Add MCP, OASF, ENS, email, or explicit cross-registry references only when the user provides them.
4. For reads, use the plugin tools directly and return both `token_uri` and `normalized_token_uri` plus the decoded registration when available.
5. For advanced writes, collect or construct a structured registration object and pass it to `erc8004_register_agent`. Do not ask the plugin to infer arbitrary prose.
6. Keep the public `/.well-known/agent-registration.json` and the on-chain registration aligned. In this repo they must be treated as two views of the same canonical model, not separate documents.
7. If the user asks to see all registered agents, use `erc8004_list_registrations` instead of assuming a hardcoded count.
