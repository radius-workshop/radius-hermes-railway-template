---
name: using-godaddy
description: Use GoDaddy domain MCP tools and GoDaddy ANS registry tools correctly.
published: true
---

# Using GoDaddy

GoDaddy has two distinct capability surfaces in this agent:

- GoDaddy MCP: domain availability checks, domain suggestions, and other registrar/domain workflows exposed by the configured `mcp_servers.godaddy` server.
- GoDaddy ANS: Agent Name Service registry registration, search, lookup, resolution, certificate, event, and verification workflows exposed by the local `godaddy-ans` plugin.

Use the MCP domain tools when the user asks about domain names, domain availability, or domain suggestions.

Use the `godaddy-ans` plugin tools when the user asks about ANS, Agent Name Service, registered agents, resolving agent hosts, registering agents, revoking agents, certificates, events, or validating ANS registrations.

Default GoDaddy ANS API calls to production. Do not use OTE unless the operator explicitly asks for it or sets `GODADDY_ANS_ENV=ote`.

Do not inspect `/app/plugins/godaddy-ans`, run `/app/scripts/godaddy/ans.py`, install packages, print or grep `GODADDY_API_KEY` / `GODADDY_API_SECRET`, or set secrets in terminal for normal GoDaddy ANS work. The plugin tools receive configured runtime credentials from the environment.

The ANS API source of truth is `https://developer.godaddy.com/swagger/swagger_ans.json`.

## Common ANS Tool Choices

- `godaddy_ans_capabilities`: explain the GoDaddy MCP versus ANS split and current Swagger-aligned payload rules.
- `godaddy_ans_search`: search ANS agents, for example `{"query": "payment"}`. Prefer `query` for natural-language search terms; it searches ANS server-side display-name and host filters, deduplicates results, and can broaden empty long-word searches.
- `godaddy_ans_get_agent`: fetch a specific ANS registration by agent id.
- `godaddy_ans_resolve`: resolve an agent host plus version. This uses `POST /v1/agents/resolution` with `agentHost` and `version` in the JSON body.
- `godaddy_ans_prepare_registration`: generate Swagger-aligned local CSR and registration payload artifacts without calling GoDaddy.
- `godaddy_ans_register`: submit this agent's Swagger-aligned registration payload to the configured GoDaddy ANS API.
- `godaddy_ans_revoke`: revoke an active agent or cancel an eligible pending registration.
- `godaddy_ans_verify_acme`: trigger ACME domain-control validation for a pending registration.
- `godaddy_ans_verify_dns`: verify final external-domain DNS records after ACME/certificate steps.
- `godaddy_ans_get_identity_certificates` / `godaddy_ans_get_server_certificates`: retrieve issued certificates.
- `godaddy_ans_submit_identity_csr` / `godaddy_ans_submit_server_csr`: submit base64 CSR payloads for certificate issuance.
- `godaddy_ans_get_csr_status`: inspect CSR processing status.
- `godaddy_ans_events`: retrieve ANS agent events.

## Search Specifics

`GET /v1/agents` supports:

- `agentDisplayName`: partial matching, max length 64.
- `agentHost`: target agent host domain.
- `version`: flexible version matching.
- `protocol`: `A2A`, `MCP`, or `HTTP-API`.
- `limit`: default 20, min 1, max 100.
- `offset`: default 0.
- `status`: `PENDING_DNS`, `ACTIVE`, `DEPRECATED`, `REVOKED`, or `ALL`. If omitted, the API defaults to `ACTIVE`. If `ALL` is included with other statuses, only `ALL` applies.

## Registration Payload Requirements

`POST /v1/agents/register` consumes an `AgentRegistrationRequest`.

Required top-level fields:

- `agentDisplayName`: human-readable name, max length 64.
- `identityCsrPEM`: base64-encoded PEM CSR for the identity certificate.
- `version`: semantic version string in `major.minor.patch` format. The registration schema pattern does not allow pre-release or build suffixes.
- `agentHost`: FQDN where the agent is hosted, max length 253.
- `endpoints`: at least one endpoint object.

Optional top-level fields:

- `agentDescription`: max length 150.
- `serverCsrPEM`: base64-encoded PEM CSR for the server certificate. Required when not using BYOC server certificate fields.
- `serverCertificatePEM`: base64-encoded PEM server certificate for BYOC.
- `serverCertificateChainPEM`: base64-encoded PEM chain for BYOC. If present, `serverCertificatePEM` must also be present.

Important:

- Identity certificates are always issued by the Registration Authority.
- BYOC is permitted for server certificates only.
- Do not put `functions` at the top level. `functions` belongs inside an endpoint object.
- Do not decode CSR fields to raw PEM strings before API submission. The Swagger requires base64-encoded PEM strings for CSR fields.

## Endpoint Requirements

Each endpoint requires:

- `agentUrl`: URI where the agent accepts requests.
- `protocol`: one of `A2A`, `MCP`, or `HTTP-API`.

Optional endpoint fields:

- `metaDataUrl`: URI for agent metadata.
- `documentationUrl`: URI for docs.
- `transports`: values from `STREAMABLE-HTTP`, `SSE`, `JSON-RPC`, `GRPC`, `REST`, `HTTP`.
- `functions`: endpoint-local functions. For MCP these are tools; for A2A these are skills; for HTTP-API these are routes.

Each function requires:

- `id`: max length 64.
- `name`: max length 64.

Optional function fields:

- `tags`: max 5 tags, each max length 20.

## ANS Name And CSR Requirements

The ANS name format is `ans://v<version>.<agentHost>`.

Both identity and server CSRs should include:

- Common Name: `<agentHost>`
- DNS SAN: `<agentHost>`
- URI SAN: `ans://v<version>.<agentHost>`

The bundled `godaddy_ans_prepare_registration` tool generates base64-encoded PEM CSR fields and includes DNS plus URI SANs. If manually generating CSRs, use an OpenSSL config file instead of relying on `-addext` for URI SANs.

### OpenSSL Config Template

```ini
[req]
default_bits = 2048
prompt = no
distinguished_name = dn
req_extensions = v3_req

[dn]
CN = __HOST__
O = Your Agent Name

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = __HOST__
URI.1 = __ANS_NAME__
```

Generate with:

```bash
HOST="your-agent-host.example.com"
ANS_NAME="ans://v1.0.0.$HOST"
DIR="/path/to/ans-state"

sed "s|__HOST__|$HOST|g; s|__ANS_NAME__|$ANS_NAME|g" template.cnf > "$DIR/identity.cnf"
cp "$DIR/identity.cnf" "$DIR/server.cnf"

openssl genrsa -out "$DIR/identity.key.pem" 2048
openssl req -new -key "$DIR/identity.key.pem" -config "$DIR/identity.cnf" -out "$DIR/identity.csr.pem"

openssl genrsa -out "$DIR/server.key.pem" 2048
openssl req -new -key "$DIR/server.key.pem" -config "$DIR/server.cnf" -out "$DIR/server.csr.pem"
```

Verify: `openssl req -in <csr> -noout -text | grep -A3 "Subject Alternative"` must show both `DNS:` and `URI:ans://`.

## Registration Flows

The API supports:

1. GoDaddy domains with CSRs.
2. External domains with CSRs and async ACME validation.
3. External domains with BYOC server certificate fields.

Registration commonly returns `202` with one of:

- `PENDING_VALIDATION`: complete ACME HTTP-01 or DNS-01, then call `godaddy_ans_verify_acme`.
- `PENDING_CERTS`: domain validation passed, certificate issuance pending.
- `PENDING_DNS`: final DNS records need to be configured, then call `godaddy_ans_verify_dns`.

After a pending response, use `godaddy_ans_get_agent(agent_id=...)` to inspect:

- `registrationPending.status`
- `registrationPending.challenges`
- `registrationPending.dnsRecords`
- `registrationPending.expiresAt`
- `registrationPending.nextSteps`

## ACME Validation Workflow

`godaddy_ans_verify_acme` triggers domain-control validation. The Registration Authority automatically determines which challenge is discoverable.

### HTTP-01 Challenge

1. Write the `keyAuthorization` string to `$HERMES_HOME/acme-challenges/<token>` using `echo -n`.
2. Verify locally: `curl http://localhost:8080/.well-known/acme-challenge/<token>`.
3. Ensure the public domain routes `/.well-known/acme-challenge/*` to the origin server. If a CDN/proxy sits in front, it must pass these paths through.
4. Call `godaddy_ans_verify_acme(agent_id=...)`.

### DNS-01 Challenge

1. Add TXT record `_acme-challenge.<agentHost>` with the provided challenge value.
2. Wait for DNS propagation.
3. Call `godaddy_ans_verify_acme(agent_id=...)`.

## Final DNS Verification

`godaddy_ans_verify_dns` is not the ACME TXT challenge verifier. It verifies the final DNS records required for external domain registration after ACME and certificate work.

The API checks all required DNS records based on the registration, including:

- HTTPS
- TLSA
- `_ans`
- `_ra-badge`

Use `godaddy_ans_get_agent(agent_id=...)` to inspect `registrationPending.dnsRecords` before calling `godaddy_ans_verify_dns`.

## Domain Selection And CAA

```
Can you modify DNS records for the domain?
├── YES → Use DNS-01 challenge or configure HTTP-01 pass-through
└── NO (e.g., *.up.railway.app, *.herokuapp.com)
    └── CAA records or platform routing may block issuance
        → Use a custom domain where you control DNS
```

Known pitfalls:

- `*.up.railway.app` can block GoDaddy CA issuance via CAA records.
- Any CDN that does not proxy `/.well-known/acme-challenge/*` to origin can make HTTP-01 fail with public 404s.
- If the public domain resolves to a CDN IP, test the local challenge URL and public challenge URL separately before calling `godaddy_ans_verify_acme`.

## Revocation

`godaddy_ans_revoke` uses `POST /v1/agents/{agentId}/revoke`.

Allowed reasons:

- `KEY_COMPROMISE`
- `CESSATION_OF_OPERATION`
- `AFFILIATION_CHANGED`
- `SUPERSEDED`
- `CERTIFICATE_HOLD`
- `PRIVILEGE_WITHDRAWN`
- `AA_COMPROMISE`

Notes:

- Active agents can be revoked.
- Eligible pending registrations can be cancelled after domain validation.
- `PENDING_VALIDATION` registrations are not cancellable through this API and expire if ACME verification is not completed.

## Certificate And Event Tools

Certificate tools:

- `godaddy_ans_get_identity_certificates`
- `godaddy_ans_submit_identity_csr`
- `godaddy_ans_get_server_certificates`
- `godaddy_ans_submit_server_csr`
- `godaddy_ans_get_csr_status`

CSR submission payloads use `csrPEM` as a base64-encoded PEM CSR. The plugin accepts raw PEM and encodes it, or accepts an already base64-encoded value.

CSR status values:

- `PENDING`
- `SIGNED`
- `REJECTED`

Events:

- `godaddy_ans_events` retrieves paginated ANS events.
- Optional `provider_id` filters by provider.
- `last_log_id` is the pagination cursor.
- Events are retained for 30 days.

## Common Error Reference

| HTTP | Cause | Fix |
|---|---|---|
| 401 | Authentication failed | Check GoDaddy API key/secret configuration |
| 403 | Authorization failed | Confirm credentials are allowed for ANS |
| 409 | Agent ID or ANS name already exists | Use a different host/version or inspect existing agent |
| 422 | Missing required registration field | Include `agentDisplayName`, `identityCsrPEM`, `version`, `agentHost`, and `endpoints` |
| 422 | Invalid version | Use SemVer `major.minor.patch` |
| 422 | Invalid search pagination | Keep `limit` between 1 and 100 and `offset` >= 0 |
| 422 | DNS records not found or incorrect | Use `registrationPending.dnsRecords` and configure HTTPS, TLSA, `_ans`, and `_ra-badge` records |
| 422 | ACME validation failed | Fix HTTP-01 pass-through or DNS-01 TXT challenge, then retry `godaddy_ans_verify_acme` |

## Complete Registration Sequence

```bash
# 1. Generate artifacts with the prepare tool
# Call godaddy_ans_prepare_registration with desired params and state_dir

# 2. Inspect the payload
DIR="/data/.hermes/godaddy/ans"
jq . "$DIR/registration-payload.json"

# Confirm:
# - top-level fields match AgentRegistrationRequest
# - identityCsrPEM and serverCsrPEM are base64 strings
# - no top-level functions field exists
# - endpoint agentUrl/metaDataUrl values use the intended agentHost
# - endpoint protocol values are A2A, MCP, or HTTP-API

# 3. Verify CSRs if needed
openssl req -in "$DIR/identity.csr.pem" -noout -text | grep -A3 "Subject Alternative"
openssl req -in "$DIR/server.csr.pem" -noout -text | grep -A3 "Subject Alternative"

# 4. Submit registration with the tool
# Call godaddy_ans_register with the same params/state_dir

# 5. If PENDING_VALIDATION, complete HTTP-01 or DNS-01
HERMES_HOME="${HERMES_HOME:-/data/.hermes}"
mkdir -p "$HERMES_HOME/acme-challenges"
echo -n '<keyAuthorization>' > "$HERMES_HOME/acme-challenges/<token>"
curl http://localhost:8080/.well-known/acme-challenge/<token>

# 6. Trigger ACME validation
# Call godaddy_ans_verify_acme(agent_id="...")

# 7. If PENDING_DNS, configure the required DNS records from get_agent
# Call godaddy_ans_verify_dns(agent_id="...")
```
