# RADIUS AGENT INSTRUCTIONS

This repository is a batteries-included Hermes template. Treat the bundled files as active project context, not as optional examples.

## Priority resources

- `HERMES.md` contains the Hermes-specific project instructions and A2A/JWT rules.
- `skills/*.md` are bundled agent skills. Read the relevant skill before using the associated capability.
- `plugins/gen-jwt` provides the `generate_a2a_token` tool used for A2A bearer tokens.
- `plugins/agent-info` provides the `get_agent_info` tool used to retrieve an agent's public discovery metadata.
- `plugins/radius-cast` provides Foundry-backed Radius wallet tools for address lookup, balances, transfers, and tx status.
- `scripts/radius/*` contains the built-in Radius wallet scripts.
- `scripts/agent_server/*` contains the HTTP agent server, A2A bridge, auth, DID, and discovery implementation.

## Expected behavior

- Prefer the built-in Radius capabilities when the user asks about payments, wallets, SBC, RUSD, Radius, or crypto flows.
- In this repository, interpret `Radius` as the Radius network / ecosystem by default. Do not default to the geometry meaning or the RADIUS AAA protocol unless the user clearly asks for those.
- If ByteRover is enabled, use it as structured long-term memory. Organize memories by session date and only persist durable, top-level topics plus wallet addresses, descriptions, and important transactions.
- For A2A auth, use `generate_a2a_token`. Do not write custom JWT signing code.
- When you need project-specific behavior, inspect the relevant skill or script in this repo before answering from memory.
- Treat this repo as intentionally prewired: skills, scripts, and plugins are part of the product surface.
