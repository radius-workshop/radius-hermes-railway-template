# Radius Hermes Railway Template 0.1.0

The first versioned template release combines a persistent Hermes deployment on
Railway with Radius wallet tools, A2A communication, public agent discovery, and
GoDaddy Agent Name Service integration.

- Identify the deployed template version through the agent card and `/livez`.
- Use the bundled `radius-cli` wallet for balances, transfers, and transaction status.
- Discover agent metadata, skills, DID identity, and available interfaces.
- Build with a pinned Hermes release and inspect the attached build manifest for
  the exact upstream revisions and Python packages used by release validation.

This template remains demo software. Automated checks cover repository tests,
container construction, HTTP discovery, liveness, and health authentication.
They do not certify a production deployment or execute external paid transactions.
