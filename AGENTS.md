# RADIUS AGENT INSTRUCTIONS

This repository is a batteries-included Hermes template. Treat the bundled files as active project context, not as optional examples.

## Priority resources

- `HERMES.md` contains the Hermes-specific project instructions and A2A/JWT rules.
- `skills/*.md` are bundled agent skills. Read the relevant skill before using the associated capability.
- `plugins/gen-jwt` provides the `generate_a2a_token` tool used for A2A bearer tokens.
- `plugins/agent-info` provides the `get_agent_info` tool used to retrieve an agent's public discovery metadata.
- `plugins/radius-cast` provides Foundry-backed Radius wallet tools for address lookup, balances, transfers, and tx status.
- `plugins/godaddy-ans` provides GoDaddy Agent Name Service registry tools such as `godaddy_ans_search`, lookup, resolution, registration, and validation, plus the narrow `godaddy_dns_set_records` domain DNS record writer.
- `scripts/radius/*` contains the built-in Radius wallet scripts.
- `scripts/godaddy/*` contains GoDaddy ANS helper scripts behind the plugin tools.
- `scripts/agent_server/*` contains the HTTP agent server, A2A bridge, auth, DID, and discovery implementation.

## Expected behavior

- Prefer the built-in Radius capabilities when the user asks about payments, wallets, SBC, RUSD, Radius, or crypto flows.
- For GoDaddy domain search, availability, and suggestions, use the GoDaddy MCP tools. For setting DNS records on a known GoDaddy-managed domain, use `godaddy_dns_set_records`.
- For GoDaddy ANS / Agent Name Service registry search, lookup, resolution, registration, or validation, use the bundled `godaddy-ans` plugin tools. Do not fall back to terminal scripts, package installs, or environment-secret inspection for normal ANS work.
- Default GoDaddy ANS API calls to production. Use OTE only when the operator explicitly asks for it or sets `GODADDY_ANS_ENV=ote`.
- For GoDaddy ANS registration, read `skills/using-godaddy.md` first. Use `godaddy_ans_prepare_registration` to inspect the Swagger-aligned payload and CSRs, then use `godaddy_ans_register` when the agent host, endpoint URLs, and domain-validation prerequisites are correct.
- In this repository, interpret `Radius` as the Radius network / ecosystem by default. Do not default to the geometry meaning or the RADIUS AAA protocol unless the user clearly asks for those.
- If ByteRover is enabled, use it as structured long-term memory. Organize memories by session date and only persist durable, top-level topics plus wallet addresses, descriptions, and important transactions.
- For A2A auth, use `generate_a2a_token`. Do not write custom JWT signing code.
- When you need project-specific behavior, inspect the relevant skill or script in this repo before answering from memory.
- Treat this repo as intentionally prewired: skills, scripts, and plugins are part of the product surface.
