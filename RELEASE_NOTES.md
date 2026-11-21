# Radius Hermes Railway Template 0.2.0

Deploy a persistent Hermes agent with Radius wallet tools, public discovery,
trusted A2A communication, optional KYA signed claims, and Slack app setup.

This release adds mainnet x402 payments, a Kubernetes chart, an egress proxy
that keeps credentials out of the agent, and three Slack features. Read
"Upgrading from 0.1.0" before you deploy it over an existing agent.

## New

- **Pay x402 sellers on Radius and Base, testnet and mainnet.** `X402_PAID_NETWORKS`
  chooses among Radius Testnet and Mainnet (SBC) and Base Sepolia and Mainnet
  (USDC). `X402_ALLOWED_HOSTS=*` allows any public HTTPS seller without approving
  each hostname first. Receipts still need independent reconciliation with the chain.
- **Run on Kubernetes.** A Helm chart in `deploy/helm/hermes-agent` deploys the
  same image as a single-replica StatefulSet with a persistent volume, an optional
  egress proxy, Ingress and NetworkPolicies. It passes the `restricted` Pod
  Security Standard.
- **Keep credentials out of the agent.** An optional egress service (iron-proxy)
  holds provider credentials and injects them into outbound requests, so the agent
  holds only tokens. `scripts/egress/doctor.py` checks a Railway setup without
  printing any value, and `scripts/egress/set_credential.py` sets a credential
  with hidden input.
- **See the wallet in Slack.** The agent's Home tab shows stablecoin balances,
  ETH for gas, spending settings and recent x402 payments. It is read-only, and
  is shown only to people allowed to talk to the agent. Switch on **Home Tab** in
  the Slack app's settings; `SLACK_HOME=false` turns it off.
- **React to Slack messages.** The `slack_react` tool lets the agent add or remove
  an emoji reaction, custom emoji included, in the conversation it is in.
- **Read the feedback people give.** 👍/👎 clicks on the agent's Slack replies are
  stored in a local ledger that the `feedback_ledger` tool reports on.
- **Slack experience settings.** Railway variables control rich blocks, feedback
  buttons and related Slack behaviour.

## Upgrading from 0.1.0

- **The container runs as uid 10001, not root.** The `/data` volume must be
  writable by that user, or the agent stops at boot with a message saying so. On
  Kubernetes set `fsGroup: 10001` (the chart does); for Docker, `chown` the volume
  to `10001:10001`.
- **x402 has no default per-call cap.** 0.1.0 capped a payment at 0.01 SBC unless
  you set `X402_MAX_PER_CALL`. Now an unset or empty value means no cap. Set a
  positive number to keep one. There is no cumulative budget.
- **x402 networks.** An install with no `X402_PAID_NETWORKS` keeps paying on Radius
  Testnet only. Mainnet payments happen only if you enable a mainnet network.
- **ByteRover is removed.** The image no longer installs the ByteRover CLI, and
  `BYTEROVER_API_KEY` and `BYTEROVER_LOCAL` are ignored. On first boot after an
  upgrade, an existing volume's `memory.provider: byterover` is reset to Hermes'
  built-in memory. Memories already stored in ByteRover are not migrated.
- **radius-cli is 0.5.0 and needs Node.js 22**, which the image provides.

## Unchanged

A2A requires explicitly trusted peer DIDs by default, JWTs must expire, and KYA
verification requires trusted HTTPS issuers. Paid A2A access remains optional and
on Radius Testnet. Telegram, Discord and Slack chat are supported; optional
Discord voice-channel support is excluded to avoid an unsafe dependency constraint.
