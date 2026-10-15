# Radius Hermes Railway Template 0.1.0

Deploy a persistent Hermes agent with Radius wallet tools, public discovery,
trusted A2A communication, optional KYA signed claims, and Slack app setup.

- **Know what is running.** The agent card and `/livez` report the template version.
  Release validation builds and checks the container and attaches its dependency
  inventory. Hermes and the Radius CLI have tested defaults.
- **Control remote access.** A2A now requires explicitly trusted peer DIDs by
  default. JWTs must expire, and outbound tokens cannot be replayed against their
  issuer. For upgrades, configure `TRUSTED_DIDS` and exchange fresh local tokens
  through `/token`. Existing self-issued tokens are revoked.
- **Try bounded x402 payments.** Approve seller hostnames with `X402_ALLOWED_HOSTS`
  before using the Radius Testnet buyer. Requests have finite per-call caps and a
  durable intent log. Delivery, reported settlement, and unknown payment outcomes
  are distinct; receipts still need independent reconciliation. Base and mainnet
  payments are excluded from this release.
- **Verify optional identity claims.** KYA verification requires trusted HTTPS
  issuers, checks audience and expiration, and prevents replay within one process.
  Signed claims do not certify regulatory compliance or settle a payment.
- **Operate from a known revision.** Manual Railway deployment supports explicit
  projects, environments and services. Bootstrap tests run offline against the
  actual skill registration code and bundled wallet tools.

Telegram, Discord and Slack chat are supported. Optional Discord voice-channel
support is excluded to avoid an unsafe dependency constraint.

This template remains demo software. Automated checks cover repository tests,
container construction, HTTP discovery, liveness and authentication. They do not
certify a production deployment or execute external paid transactions. The
container retains terminal access and should not be entrusted with real funds.
