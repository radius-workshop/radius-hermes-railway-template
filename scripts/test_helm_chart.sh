#!/usr/bin/env bash
# End-to-end check of deploy/helm/hermes-agent on a local Kubernetes cluster.
#
#   scripts/test_helm_chart.sh                     # build image, create kind cluster, test, clean up
#   BUILD=false IMAGE=radius-hermes:ci scripts/test_helm_chart.sh   # reuse an image
#   CLUSTER=current scripts/test_helm_chart.sh     # use the current kubectl context
#   KEEP=true scripts/test_helm_chart.sh           # leave the release (and kind cluster) running
#
# Installs the chart with the egress proxy and NetworkPolicies on, in a namespace
# that enforces the "restricted" Pod Security Standard, using fake credentials.
# No external network access is needed: the agent boots, serves discovery, and
# its chat gateway keeps retrying upstreams it cannot reach.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-radius-hermes:chart-test}"
BUILD="${BUILD:-true}"
CLUSTER="${CLUSTER:-kind}"            # kind | current
KIND_NAME="${KIND_NAME:-hermes-chart}"
NS="${NAMESPACE:-hermes-chart-test}"
RELEASE="${RELEASE:-hermes}"
KEEP="${KEEP:-false}"
TIMEOUT="${TIMEOUT:-10m}"
CHECK_NETWORK_POLICY="${CHECK_NETWORK_POLICY:-true}"
CHART=deploy/helm/hermes-agent

log() { printf '\n[chart-test] %s\n' "$*"; }
fail() { printf '\n[chart-test] FAIL: %s\n' "$*" >&2; exit 1; }

for tool in helm kubectl; do
  command -v "$tool" >/dev/null || fail "$tool is required"
done

created_cluster=false
work=""
cleanup() {
  status=$?
  [[ -n "$work" ]] && rm -rf "$work"
  if [[ $status -ne 0 ]]; then
    log "Diagnostics"
    kubectl -n "$NS" get pods,pvc,svc,networkpolicy -o wide 2>/dev/null || true
    kubectl -n "$NS" describe pods 2>/dev/null | tail -60 || true
    kubectl -n "$NS" logs "statefulset/${RELEASE}-hermes-agent" --tail=120 2>/dev/null || true
    kubectl -n "$NS" logs "deployment/${RELEASE}-hermes-agent-egress" --tail=40 2>/dev/null || true
  fi
  if [[ "$KEEP" != "true" ]]; then
    helm uninstall "$RELEASE" -n "$NS" >/dev/null 2>&1 || true
    kubectl delete namespace "$NS" --wait=false >/dev/null 2>&1 || true
    if $created_cluster; then kind delete cluster --name "$KIND_NAME" >/dev/null 2>&1 || true; fi
  fi
  exit $status
}
trap cleanup EXIT

if [[ "$BUILD" == "true" ]]; then
  log "Building $IMAGE"
  docker build --build-arg VCS_REF="$(git rev-parse HEAD)" -t "$IMAGE" .
fi

if [[ "$CLUSTER" == "kind" ]]; then
  command -v kind >/dev/null || fail "kind is required (or set CLUSTER=current)"
  if ! kind get clusters 2>/dev/null | grep -qx "$KIND_NAME"; then
    log "Creating kind cluster $KIND_NAME"
    kind create cluster --name "$KIND_NAME" --wait 120s
    created_cluster=true
  fi
  kubectl config use-context "kind-$KIND_NAME" >/dev/null
  log "Loading $IMAGE into kind"
  kind load docker-image "$IMAGE" --name "$KIND_NAME"
fi

log "Rendering and linting the chart"
helm lint "$CHART" --set agent.existingSecret=x >/dev/null

log "Creating namespace $NS (Pod Security: restricted)"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl label namespace "$NS" --overwrite \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/warn=restricted >/dev/null

FAKE_PROVIDER_KEY="sk-or-v1-chart-test-not-a-real-key"
work="$(mktemp -d)"

create_secrets() {
  log "Generating an egress CA and token seed"
  local gen_ca='
import json, secrets, sys
sys.path.insert(0, sys.argv[1])
import iron_egress
cert, key = iron_egress.generate_ca()
json.dump({"seed": secrets.token_urlsafe(48), "cert": cert, "key": key}, sys.stdout)
'
  if command -v docker >/dev/null && docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker run --rm --entrypoint python3 "$IMAGE" -c "$gen_ca" /app/scripts/egress > "$work/ca.json"
  else
    python3 -c "$gen_ca" scripts/egress > "$work/ca.json"
  fi
  python3 - "$work" <<'PY'
import json, pathlib, sys
work = pathlib.Path(sys.argv[1])
data = json.loads((work / "ca.json").read_text())
(work / "seed").write_text(data["seed"])
(work / "ca.crt").write_text(data["cert"] + "\n")
(work / "ca.key").write_text(data["key"] + "\n")
PY
  log "Creating Secrets"
  kubectl -n "$NS" create secret generic hermes-agent \
    --from-file=EGRESS_TOKEN_SEED="$work/seed" \
    --from-file=EGRESS_CA_CERT="$work/ca.crt" \
    --from-literal=JWT_API_KEY=chart-test-api-key >/dev/null
  kubectl -n "$NS" create secret generic hermes-egress \
    --from-file=EGRESS_TOKEN_SEED="$work/seed" \
    --from-file=EGRESS_CA_CERT="$work/ca.crt" \
    --from-file=EGRESS_CA_KEY="$work/ca.key" \
    --from-literal=OPENROUTER_API_KEY="$FAKE_PROVIDER_KEY" \
    --from-literal=TELEGRAM_BOT_TOKEN=123456:chart-test-not-a-real-token >/dev/null
}

# Reruns with KEEP=true reuse the Secrets: a new CA would not reach pods that
# are already running, because editing a Secret does not restart them.
if ! kubectl -n "$NS" get secret hermes-agent hermes-egress >/dev/null 2>&1; then
  create_secrets
fi

repo="${IMAGE%:*}"
tag="${IMAGE##*:}"
[[ "$repo" == "$IMAGE" ]] && tag=latest

log "Installing release $RELEASE"
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" --wait --timeout "$TIMEOUT" \
  -f "$CHART/examples/with-egress.yaml" \
  --set image.repository="$repo" --set image.tag="$tag" --set image.pullPolicy=Never \
  --set persistence.size=1Gi \
  --set networkPolicy.enabled=true \
  --set agent.config.TELEGRAM_ALLOWED_USERS=1 \
  --set agent.config.RADIUS_AUTO_FUND=false

AGENT="${RELEASE}-hermes-agent-0"
agent_exec() { kubectl -n "$NS" exec "$AGENT" -c agent -- "$@"; }

log "helm test (in-cluster discovery check)"
helm test "$RELEASE" -n "$NS" --logs --timeout 3m

log "Checking the agent runs non-root with a read-only root filesystem"
uid="$(agent_exec id -u)"
[[ "$uid" == "10001" ]] || fail "agent runs as uid $uid, expected 10001"
if agent_exec touch /app/chart-test-write 2>/dev/null; then fail "root filesystem is writable"; fi
agent_exec test -w /data/.hermes || fail "/data is not writable by the agent"

log "Checking the agent holds proxy tokens, not the real provider key"
envfile="$(agent_exec cat /data/.hermes/.env)"
grep -q '^OPENROUTER_API_KEY=sk-or-v1-hermes-egress-' <<<"$envfile" || fail "agent .env lacks the egress proxy token"
if grep -q "$FAKE_PROVIDER_KEY" <<<"$envfile"; then fail "real provider key reached the agent"; fi
agent_exec python3 -c "
import os, socket
from urllib.parse import urlsplit
u = urlsplit(os.environ['EGRESS_PROXY_URL'])
socket.create_connection((u.hostname, u.port), timeout=5).close()
" || fail "agent cannot reach the egress proxy"
# Without the proxy token, iron-proxy refuses a secret's own host with 403. Getting
# that status proves the agent trusts the egress CA (TLS is intercepted).
code="$(agent_exec sh -c 'curl -sS -o /dev/null -w "%{http_code}" --cacert /data/.hermes/egress/ca-bundle.crt -x "$EGRESS_PROXY_URL" https://openrouter.ai/api/v1/models')" \
  || fail "TLS through the egress proxy failed (CA not trusted?)"
[[ "$code" == "403" ]] || fail "expected the proxy to refuse an untokened request with 403, got $code"

if [[ "$CHECK_NETWORK_POLICY" == "true" ]]; then
  # The agent may reach cluster DNS on port 53 only. CoreDNS's readiness port
  # (8181) on its pod IP is a private, in-cluster address that must be blocked.
  log "Checking NetworkPolicy blocks other in-cluster traffic"
  dns_ip="$(kubectl -n kube-system get pods -l k8s-app=kube-dns -o jsonpath='{.items[0].status.podIP}')"
  [[ -n "$dns_ip" ]] || fail "could not find a kube-dns pod"
  if agent_exec python3 -c "
import socket, sys
socket.create_connection((sys.argv[1], 8181), timeout=5).close()
" "$dns_ip" 2>/dev/null; then
    fail "agent reached $dns_ip:8181; NetworkPolicy is not enforced (set CHECK_NETWORK_POLICY=false if your CNI ignores it)"
  fi
fi

log "Checking the wallet and identity survive a pod restart (persistent /data)"
did_doc() { agent_exec curl -fsS "http://127.0.0.1:3000/.well-known/did.json"; }
wallet_before="$(agent_exec cat /data/.hermes/.radius-cli/address)"
[[ "$wallet_before" =~ ^0x[0-9a-fA-F]{40}$ ]] || fail "radius-cli wallet was not created: '$wallet_before'"
did_before="$(did_doc)"
kubectl -n "$NS" delete pod "$AGENT" --wait=true >/dev/null
kubectl -n "$NS" rollout status "statefulset/${RELEASE}-hermes-agent" --timeout "$TIMEOUT"
kubectl -n "$NS" wait --for=condition=Ready "pod/$AGENT" --timeout "$TIMEOUT" >/dev/null
wallet_after="$(agent_exec cat /data/.hermes/.radius-cli/address)"
did_after="$(did_doc)"
[[ "$wallet_before" == "$wallet_after" ]] || fail "wallet changed across restart: $wallet_before -> $wallet_after"
[[ "$did_before" == "$did_after" ]] || fail "DID document (keys) changed across restart"

log "PASS: chart installed under Pod Security 'restricted'; discovery, egress isolation, NetworkPolicy and persistence verified (wallet $wallet_after)"
