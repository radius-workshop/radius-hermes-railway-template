# hermes-agent Helm chart

Runs this template on any Kubernetes cluster (1.25+) with core objects only:
a single-replica StatefulSet with a persistent `/data` volume, a Service, a
ConfigMap, and Secrets you provide. The optional egress proxy, Ingress and
NetworkPolicies are standard Kubernetes too. The chart needs no CRDs and no
cloud APIs.

It runs the same image and entrypoint as Railway. Railway Variables become
`agent.config` (plain settings) and a Secret (credentials).

## Quick start

```sh
kubectl create namespace hermes
kubectl -n hermes create secret generic hermes-agent \
  --from-literal=OPENROUTER_API_KEY=sk-or-v1-... \
  --from-literal=TELEGRAM_BOT_TOKEN=123456:...
helm install hermes deploy/helm/hermes-agent -n hermes \
  -f deploy/helm/hermes-agent/examples/minimal.yaml \
  --set image.repository=<registry>/radius-hermes --set image.tag=<tag>
helm test hermes -n hermes
```

The image isn't published to a registry yet. Build it from the repository root
(`docker build -t <registry>/radius-hermes:<tag> .`) and push it, or load it
into a local cluster (`kind load docker-image ...`).

The entrypoint validates the Secret on boot, as on Railway: you need a provider
(`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` plus
`OPENAI_BASE_URL`) and at least one platform (Telegram, Discord, or both Slack
tokens). Watch the logs with `kubectl -n hermes logs -f statefulset/hermes-hermes-agent`.

## What it deploys

| Object | Notes |
|---|---|
| StatefulSet, 1 replica | The agent and its chat gateway. Never scale it: two gateways answer every message twice, and the state lives on one ReadWriteOnce volume. The schema rejects a replica count. |
| PVC `data` → `/data` | Hermes state, wallet keystore, skills, workspace. Kept when the release is deleted. |
| Service `:3000` | Discovery (`/.well-known/*`), `/a2a`, `/token`, homepage |
| ConfigMap | `agent.config` plus chart-managed `PORT`, `PUBLIC_URL` and egress wiring. Empty values are dropped. |
| ServiceAccount | Token not mounted. Add workload-identity annotations here. |
| Egress Deployment and Service (optional) | iron-proxy with the real API keys. See [Egress proxy](../../../README.md#egress-proxy-with-secret-injection). |
| Ingress, NetworkPolicies (optional) | Standard `networking.k8s.io/v1` |
| Test pod (`helm test`) | Checks `/livez`, the agent card and `did.json` |

## Security defaults

These work on every cluster and pass the `restricted` Pod Security Standard:

- uid/gid 10001 with `fsGroup` on `/data`, and `runAsNonRoot`
- read-only root filesystem (`/tmp` is an emptyDir)
- all capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp
- no ServiceAccount token, no service-link environment variables
- real secrets only in Secrets; with the egress proxy, provider keys exist only in the egress pod

The image still runs as root by default so Railway's root-owned volumes keep
working. The chart sets the UID.

## Configuration

See [values.yaml](values.yaml) for every key. The ones you'll set most:

| Key | Purpose |
|---|---|
| `agent.existingSecret` | Secret with credentials (`envFrom`). Required. |
| `agent.config` | Non-secret settings: allowlists, `LLM_MODEL`, Slack options, ... |
| `publicUrl` | `PUBLIC_URL`. Defaults to `https://<ingress.host>`. The `did:web` identity derives from it. |
| `image.repository` / `tag` / `digest` | Pin by digest in production |
| `persistence.storageClass` / `size` / `existingClaim` | Storage |
| `ingress.*` | Public routing (`className`, `annotations`, `host`, `tlsSecretName`) |
| `egress.enabled` / `existingSecret` / `secrets` | Egress proxy with secret injection |
| `networkPolicy.enabled` | Restrict traffic (needs a CNI that enforces NetworkPolicy) |

`secrets.create` renders Secrets from values for local testing only. The values
end up in the Helm release history.

A changed Secret or ConfigMap from outside the chart doesn't restart pods. Run
`kubectl rollout restart statefulset/<release>-hermes-agent`, or use a
controller such as Reloader. Changes made through the chart's own values roll
the pods automatically.

## Egress proxy

```yaml
egress:
  enabled: true
  existingSecret: hermes-egress      # EGRESS_TOKEN_SEED, EGRESS_CA_CERT, EGRESS_CA_KEY + real keys
  secrets: [OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN]
agent:
  existingSecret: hermes-agent       # EGRESS_TOKEN_SEED, EGRESS_CA_CERT, no real keys
```

Generate the CA and seed with `python3 scripts/egress/iron_egress.py gen-ca`. The
chart sets `EGRESS_PROXY_URL` and `EGRESS_SECRETS` on both pods. The agent
refuses to boot if its Secret still holds a real value for a managed key. See
[examples/with-egress.yaml](examples/with-egress.yaml).

## NetworkPolicy

With `networkPolicy.enabled=true`:

- **Agent ingress:** only the HTTP port, from `networkPolicy.ingressFrom` peers (any source if the list is empty).
- **Agent egress:** cluster DNS, the egress proxy (when it's on), and, while `agentDirectEgress` is true, public internet addresses. Private, link-local and metadata ranges in `privateCidrs` are excluded.
- **Egress proxy:** ingress only from the agent; egress to DNS and public internet addresses.

Discord's gateway token, Radius RPC, the faucet and x402 connect directly, not
through the proxy. Keep `agentDirectEgress` on unless you allow those
destinations some other way (`extraAgentEgress`, or an FQDN policy from your CNI
in `extraObjects`).

## Adding platform integrations later

The chart deliberately leaves cloud-specific pieces out and exposes hooks
instead. Each row needs values only, not a fork:

| Concern | Hook | Example |
|---|---|---|
| Secret managers | `agent.existingSecret` + `extraObjects` | An `ExternalSecret` that creates the Secret: [examples/external-secrets.yaml](examples/external-secrets.yaml). Same pattern for `SealedSecret` or a CSI `SecretProviderClass`. |
| Vault Agent, sidecars | `agent.podAnnotations`, `agent.extraContainers`, `agent.extraVolumes`/`extraVolumeMounts` | Annotation-based injectors |
| Cloud identity | `serviceAccount.annotations` | `iam.gke.io/gcp-service-account`, `eks.amazonaws.com/role-arn`, `azure.workload.identity/client-id` |
| TLS, WAF, rate limits | `ingress.className`, `ingress.annotations` | cert-manager or controller annotations |
| Gateway API | `extraObjects` | An `HTTPRoute` pointing at the `<release>-hermes-agent` Service, port 3000 |
| Stricter egress | `networkPolicy.extraAgentEgress`, `extraObjects` | CIDR rules, Cilium or Calico FQDN policies |
| Sandboxing | `agent.runtimeClassName`, `nodeSelector`, `tolerations` | `gvisor`, `kata` |
| Backups | `persistence.storageClass`, `persistence.annotations`, `extraObjects` | Encrypted StorageClass, Velero, `VolumeSnapshot` |
| Image policy | `image.digest`, `imagePullSecrets` | Admission policies that verify digests or signatures |
| Anything else | `extraObjects` (rendered with `tpl`), `helm --post-renderer` | Raw manifests, Kustomize patches |

## Moving from Railway

1. Copy `/data` from the Railway service (for example `tar` over `railway ssh`) into the PVC. One way is to install with `persistence.existingClaim` pointing at a PVC you've restored. The wallet keystore under `/data/.hermes/.radius-cli` is the agent's on-chain identity, so copy it intact. Files owned by root are fine: `fsGroupChangePolicy: OnRootMismatch` hands them to gid 10001 on first mount, where the storage driver supports `fsGroup`.
2. Keep the same public domain. The `did:web` identity is derived from `PUBLIC_URL`, and peers trust that DID through `TRUSTED_DIDS`.
3. Move Railway Variables: credentials into the Secret, everything else into `agent.config`. Don't set `PORT`, `PUBLIC_URL`, `EGRESS_PROXY_URL` or `EGRESS_SECRETS`, because the chart manages them.
4. Stop the Railway service before starting the chart, so two gateways never run at once.

## Testing

```sh
python scripts/run_tests.py tests/test_helm_chart.py   # render checks (needs helm)
scripts/test_helm_chart.sh                             # full install on kind
```

`scripts/test_helm_chart.sh` builds the image, creates a kind cluster and
installs the chart with the egress proxy and NetworkPolicies. It runs in a
namespace that enforces Pod Security `restricted`, with fake credentials and no
external network access. It then checks `helm test`, non-root and read-only
root, that the agent holds only proxy tokens and trusts the proxy CA, that
NetworkPolicy is enforced, and that the wallet and DID survive a pod restart.
Use `CLUSTER=current` for another local cluster (minikube, k3s, Docker
Desktop), `BUILD=false IMAGE=...` to reuse an image, and `KEEP=true` to leave
it running.
