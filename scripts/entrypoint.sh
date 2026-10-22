#!/usr/bin/env bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/data/.hermes}"
export HOME="${HOME:-/data}"
export MESSAGING_CWD="${MESSAGING_CWD:-/data/workspace}"

INIT_MARKER="${HERMES_HOME}/.initialized"
ENV_FILE="${HERMES_HOME}/.env"
CONFIG_FILE="${HERMES_HOME}/config.yaml"

mkdir -p "${HERMES_HOME}" "${HERMES_HOME}/logs" "${HERMES_HOME}/sessions" "${HERMES_HOME}/cron" "${HERMES_HOME}/pairing" "${MESSAGING_CWD}"
mkdir -p "${HOME}/.claude"

# Write Claude Code settings — always overwrite to keep permissions fresh
cat > "${HOME}/.claude/settings.json" <<'EOF'
{
  "permissions": {
    "allow": [
      "Bash(curl * railway.app*)",
      "Bash(curl *railway.app*)",
      "Bash(curl * /a2a*)",
      "Bash(curl * /.well-known/*)",
      "Bash(curl * /token*)",
      "Bash(python3 /app/scripts/agent_server/gen_jwt.py*)"
    ]
  }
}
EOF

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

TAIL_PIDS=()

start_log_forwarder() {
  local file_path="$1"
  local stream="${2:-stdout}"
  local label
  label="$(basename "$file_path")"

  touch "$file_path"

  if [[ "$stream" == "stderr" ]]; then
    (
      tail -n 0 -F "$file_path" 2>/dev/null \
        | while IFS= read -r line; do
            printf '[hermes:%s] %s\n' "$label" "$line" >&2
          done
    ) &
  else
    (
      tail -n 0 -F "$file_path" 2>/dev/null \
        | while IFS= read -r line; do
            printf '[hermes:%s] %s\n' "$label" "$line"
          done
    ) &
  fi

  TAIL_PIDS+=("$!")
}

start_hermes_log_forwarders() {
  if ! is_true "${HERMES_FORWARD_LOG_FILES:-true}"; then
    echo "[bootstrap] Hermes log forwarding disabled."
    return
  fi

  local logs_dir="${HERMES_HOME}/logs"
  mkdir -p "$logs_dir"

  echo "[bootstrap] Forwarding Hermes log files from ${logs_dir} to Railway stdout/stderr..."
  start_log_forwarder "${logs_dir}/agent.log" "stdout"
  start_log_forwarder "${logs_dir}/errors.log" "stderr"

  if is_true "${HERMES_FORWARD_GATEWAY_LOG:-false}"; then
    echo "[bootstrap] Forwarding gateway.log as an additional Hermes log stream."
    start_log_forwarder "${logs_dir}/gateway.log" "stdout"
  fi
}

cleanup() {
  local exit_code="${1:-0}"

  for pid in "${TAIL_PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done

  if [[ -n "${GATEWAY_PID:-}" ]]; then
    kill "$GATEWAY_PID" 2>/dev/null || true
  fi

  if [[ -n "${AGENT_PID:-}" ]]; then
    kill "$AGENT_PID" 2>/dev/null || true
  fi

  if [[ -n "${CF_X402_PID:-}" ]]; then
    kill "$CF_X402_PID" 2>/dev/null || true
    pkill -f "cf_x402_proxy/proxy.mjs" 2>/dev/null || true
  fi

  wait 2>/dev/null || true
  exit "$exit_code"
}

validate_platforms() {
  local count=0

  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    count=$((count + 1))
  fi

  if [[ -n "${DISCORD_BOT_TOKEN:-}" ]]; then
    count=$((count + 1))
  fi

  if [[ -n "${SLACK_BOT_TOKEN:-}" || -n "${SLACK_APP_TOKEN:-}" ]]; then
    if [[ -z "${SLACK_BOT_TOKEN:-}" || -z "${SLACK_APP_TOKEN:-}" ]]; then
      echo "[bootstrap] ERROR: Slack requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN." >&2
      exit 1
    fi
    count=$((count + 1))
  fi

  if [[ "$count" -lt 1 ]]; then
    echo "[bootstrap] ERROR: Configure at least one platform: Telegram, Discord, or Slack." >&2
    exit 1
  fi
}

has_valid_provider_config() {
  # Cloudflare AI Gateway paid per call over x402 (Base USDC) through the local cf_x402_proxy.
  if [[ "${RADIUS_INFERENCE_PROVIDER:-}" == "cloudflare-x402" ]]; then
    if [[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" && -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
      return 0
    fi
    echo "[bootstrap] ERROR: RADIUS_INFERENCE_PROVIDER=cloudflare-x402 needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN." >&2
    return 1
  fi

  if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    return 0
  fi

  if [[ -n "${OPENAI_BASE_URL:-}" && -n "${OPENAI_API_KEY:-}" ]]; then
    return 0
  fi

  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    return 0
  fi

  return 1
}

append_if_set() {
  local key="$1"
  local val="${!key:-}"
  if [[ -n "$val" ]]; then
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

case "${RADIUS_INFERENCE_PROVIDER:-}" in
  ""|default|cloudflare-x402) ;;
  *)
    echo "[bootstrap] ERROR: RADIUS_INFERENCE_PROVIDER must be unset, 'default' or 'cloudflare-x402' (got '${RADIUS_INFERENCE_PROVIDER}')." >&2
    exit 1
    ;;
esac

if ! has_valid_provider_config; then
  echo "[bootstrap] ERROR: Configure a provider: OPENROUTER_API_KEY, or OPENAI_BASE_URL+OPENAI_API_KEY, or ANTHROPIC_API_KEY, or RADIUS_INFERENCE_PROVIDER=cloudflare-x402 with CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN." >&2
  exit 1
fi

validate_platforms

# === Radius CLI: ensure persistent wallet home ===
# radius-cli owns the keystore and chain config. It reads RADIUS_HOME,
# RADIUS_NETWORK, RADIUS_RPC_URL and RADIUS_SBC_ADDRESS from the environment
# and derives the RPC URL and SBC address from the network when they are unset.
RADIUS_HOME="${RADIUS_HOME:-${HERMES_HOME}/.radius-cli}"
RADIUS_ADDR_FILE="${RADIUS_HOME}/address"
mkdir -p "${RADIUS_HOME}"
export RADIUS_HOME
# radius-cli defaults to mainnet; this template defaults to testnet.
export RADIUS_NETWORK="${RADIUS_NETWORK:-testnet}"
# radius-cli treats an empty override as set, so drop blank ones.
for key in RADIUS_RPC_URL RADIUS_SBC_ADDRESS; do
  val="${!key:-}"
  if [[ -z "${val//[[:space:]]/}" ]]; then
    unset "$key"
  fi
done
if [[ -z "${RADIUS_WALLET_ADDRESS:-}" && -f "$RADIUS_ADDR_FILE" ]]; then
  export RADIUS_WALLET_ADDRESS="$(cat "$RADIUS_ADDR_FILE")"
fi

# === Agent server: auto-generate HERMES_API_KEY if not provided ===
HERMES_API_KEY_FILE="${HERMES_HOME}/.hermes_api_key"
if [[ -z "${HERMES_API_KEY:-}" && -n "${API_SERVER_KEY:-}" ]]; then
  export HERMES_API_KEY="${API_SERVER_KEY}"
  echo "[bootstrap] Using API_SERVER_KEY as HERMES_API_KEY for the A2A direct bridge."
fi
if [[ -z "${HERMES_API_KEY:-}" && -f "$HERMES_API_KEY_FILE" ]]; then
  export HERMES_API_KEY="$(cat "$HERMES_API_KEY_FILE")"
  echo "[bootstrap] Loaded stored Hermes API key."
fi
if [[ -z "${HERMES_API_KEY:-}" ]]; then
  export HERMES_API_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  echo "$HERMES_API_KEY" > "$HERMES_API_KEY_FILE"
  chmod 600 "$HERMES_API_KEY_FILE"
  echo "[bootstrap] Generated new Hermes API key."
fi

echo "[bootstrap] Writing runtime env to ${ENV_FILE}"
{
  echo "# Managed by entrypoint.sh"
  echo "HERMES_HOME=${HERMES_HOME}"
  echo "MESSAGING_CWD=${MESSAGING_CWD}"
} > "$ENV_FILE"

for key in \
  OPENROUTER_API_KEY OPENAI_API_KEY OPENAI_BASE_URL ANTHROPIC_API_KEY LLM_MODEL HERMES_INFERENCE_PROVIDER HERMES_PORTAL_BASE_URL NOUS_INFERENCE_BASE_URL HERMES_NOUS_MIN_KEY_TTL_SECONDS HERMES_DUMP_REQUESTS \
  TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS TELEGRAM_ALLOW_ALL_USERS TELEGRAM_HOME_CHANNEL TELEGRAM_HOME_CHANNEL_NAME \
  DISCORD_BOT_TOKEN DISCORD_ALLOWED_USERS DISCORD_ALLOW_ALL_USERS DISCORD_HOME_CHANNEL DISCORD_HOME_CHANNEL_NAME DISCORD_REQUIRE_MENTION DISCORD_FREE_RESPONSE_CHANNELS \
  SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_ALLOW_ALL_USERS SLACK_HOME_CHANNEL SLACK_HOME_CHANNEL_NAME SLACK_ALLOWED_CHANNELS SLACK_STRICT_MENTION SLACK_IGNORE_OTHER_USER_MENTIONS SLACK_ALLOW_BOTS SLACK_REQUIRE_MENTION SLACK_REACTION_TRIGGERS WHATSAPP_ENABLED WHATSAPP_ALLOWED_USERS \
  GATEWAY_ALLOW_ALL_USERS \
  FIRECRAWL_API_KEY NOUS_API_KEY BROWSERBASE_API_KEY BROWSERBASE_PROJECT_ID BROWSERBASE_PROXIES BROWSERBASE_ADVANCED_STEALTH BROWSER_SESSION_TIMEOUT BROWSER_INACTIVITY_TIMEOUT FAL_KEY ELEVENLABS_API_KEY VOICE_TOOLS_OPENAI_KEY \
  TINKER_API_KEY WANDB_API_KEY RL_API_URL GITHUB_TOKEN BYTEROVER_API_KEY BYTEROVER_LOCAL LINEAR_API_KEY LINEAR_TEAM_ID LINEAR_PROJECT_ID \
  TERMINAL_BACKEND TERMINAL_DOCKER_IMAGE TERMINAL_SINGULARITY_IMAGE TERMINAL_MODAL_IMAGE TERMINAL_CWD TERMINAL_TIMEOUT TERMINAL_LIFETIME_SECONDS TERMINAL_CONTAINER_CPU TERMINAL_CONTAINER_MEMORY TERMINAL_CONTAINER_DISK TERMINAL_CONTAINER_PERSISTENT TERMINAL_SANDBOX_DIR TERMINAL_SSH_HOST TERMINAL_SSH_USER TERMINAL_SSH_PORT TERMINAL_SSH_KEY SUDO_PASSWORD \
  WEB_TOOLS_DEBUG VISION_TOOLS_DEBUG MOA_TOOLS_DEBUG IMAGE_TOOLS_DEBUG CONTEXT_COMPRESSION_ENABLED CONTEXT_COMPRESSION_THRESHOLD CONTEXT_COMPRESSION_MODEL HERMES_MAX_ITERATIONS HERMES_TOOL_PROGRESS HERMES_TOOL_PROGRESS_MODE \
  RADIUS_HOME RADIUS_WALLET_ADDRESS RADIUS_NETWORK RADIUS_RPC_URL RADIUS_SBC_ADDRESS RADIUS_CHAIN_ID RADIUS_EXPLORER_URL RADIUS_CLI_BIN RADIUS_AUTO_FUND RADIUS_FAUCET_URL \
  X402_MAX_PER_CALL X402_ALLOWED_HOSTS X402_DEMO_BASE_URL X402_LEDGER_PATH \
  ERC8004_NETWORK ERC8004_TESTNET_RPC_URL ERC8004_TESTNET_REGISTRY ERC8004_TESTNET_EXPLORER_URL ERC8004_TESTNET_CHAIN_ID ERC8004_MAINNET_RPC_URL ERC8004_MAINNET_REGISTRY ERC8004_MAINNET_EXPLORER_URL ERC8004_MAINNET_CHAIN_ID ERC8004_GAS_LIMIT ERC8004_MAX_AGENT_URI_BYTES \
  AGENT_NAME AGENT_DESCRIPTION AGENT_IMAGE AGENT_ACTIVE AGENT_X402_SUPPORT AGENT_SUPPORTED_TRUST AGENT_A2A_VERSION AGENT_ERC8004_ID AGENT_ERC8004_REGISTRY AGENT_ANS_NAME AGENT_ANS_AGENT_ID AGENT_ANS_HOST AGENT_ANS_STATUS AGENT_WALLET AGENT_EMAIL AGENT_ENS \
  WEBHOOK_PORT WEBHOOK_SECRET DEBUG_SKILLS \
  EXPECTED_VENDORED_SKILLS STRICT_VENDORED_SKILLS VENDORED_SKILLS_SOURCE \
  RADIUS_SKILLS_AUTO_UPDATE RADIUS_SKILLS_REPO RADIUS_SKILLS_BRANCH RADIUS_SKILLS_WEBHOOK_SECRET RADIUS_SKILLS_GITHUB_TOKEN RADIUS_SKILLS_DIR RADIUS_SKILLS_SYNC_TIMEOUT_SECONDS RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE \
  HERMES_API_KEY HERMES_URL A2A_BRIDGE_MODEL HERMES_TIMEOUT A2A_MODE A2A_PUBLIC_URL A2A_FILE_SERVE_PATHS
do
  append_if_set "$key"
done

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "[bootstrap] Creating ${CONFIG_FILE}"
  cat > "$CONFIG_FILE" <<EOF
model: ${LLM_MODEL:-openai/gpt-5.4-nano}
terminal:
  backend: ${TERMINAL_BACKEND:-local}
  cwd: ${TERMINAL_CWD:-/data/workspace}
  timeout: ${TERMINAL_TIMEOUT:-180}
compression:
  enabled: true
  threshold: 0.85
EOF
fi

# === A2A: inject webhook route when WEBHOOK_SECRET is configured ===
# Always overwrite on every deploy so the route config stays fresh.
if [[ -n "${WEBHOOK_SECRET:-}" ]]; then
  echo "[bootstrap] Configuring A2A webhook route in config.yaml..."
  python3 - <<'PYEOF'
import yaml, os, sys
cfg_file = os.environ['HERMES_HOME'] + '/config.yaml'
secret = os.environ['WEBHOOK_SECRET']
try:
    with open(cfg_file) as f:
        cfg = yaml.safe_load(f) or {}
except Exception:
    cfg = {}
platforms = cfg.setdefault('platforms', {})
webhook = platforms.setdefault('webhook', {})
webhook['enabled'] = True
extra = webhook.setdefault('extra', {})
extra['secret'] = secret
routes = extra.setdefault('routes', {})
# Always overwrite the a2a route to keep it fresh
routes['a2a'] = {
    'secret': secret,
    'prompt': (
        'You have received an A2A message from agent {issuer_did}:\n\n'
        'Session metadata:\n'
        '- session_id: {a2a_session_id}\n'
        '- goal: {a2a_session_goal}\n'
        '- observed_turns: {a2a_session_turn_count}\n'
        '- auto_continue: {a2a_session_auto_continue}\n\n'
        '{text}\n\n'
        'To reply, follow these steps exactly:\n'
        '1. Use send_a2a_message() as the default and approved reply path for A2A conversations in this environment. '
        'It is safe to use for normal agent-to-agent communication and should be preferred over shell commands.\n'
        '2. Call send_a2a_message with agent set to {issuer_did_url}, task set to your reply, and context_id set to {context_id} '
        'so the conversation continues on the same A2A thread.\n'
        '3. Do NOT use curl, requests, or any other raw HTTP command for normal A2A replies. '
        'Only use raw transport commands if the user explicitly asks you to debug the transport layer.\n'
        '4. Continue the conversation — treat this as an ongoing dialogue, not a one-shot reply.\n'
        '5. Keep the reply concise, advance the work, and end with one concrete question unless the work is complete.'
    )
}
with open(cfg_file, 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
print('[bootstrap] A2A webhook route configured in config.yaml.')
PYEOF
  if [[ $? -ne 0 ]]; then
    echo "[bootstrap] WARNING: Could not auto-configure A2A webhook route. Add it manually to config.yaml." >&2
  fi
fi

# Ensure model is set in config.yaml (handles existing installs and model changes).
# Only a one-line `model: <name>`: a `model:` mapping (the cloudflare-x402 provider block below,
# or one an operator wrote) is left alone, since rewriting its first line would break the YAML.
if [[ -n "${LLM_MODEL:-}" ]]; then
  if grep -Eq "^model:[[:space:]]*[^[:space:]#]" "$CONFIG_FILE" 2>/dev/null; then
    sed -i -E "s|^model:[[:space:]]*[^[:space:]#].*|model: ${LLM_MODEL}|" "$CONFIG_FILE"
  elif ! grep -q "^model:" "$CONFIG_FILE" 2>/dev/null; then
    sed -i "1s|^|model: ${LLM_MODEL}\n|" "$CONFIG_FILE"
  fi
fi

# === Inference provider: Cloudflare AI Gateway over x402, re-applied on every boot ===
# RADIUS_INFERENCE_PROVIDER=cloudflare-x402 points Hermes' main model at the local cf_x402_proxy
# (127.0.0.1), which pays Cloudflare per call in Base USDC. The model value it replaces is saved
# and restored when the variable is unset or set to "default", so switching back is config-only.
CF_X402_PORT="${CF_X402_PORT:-8650}"
CF_X402_PROXY_KEY_FILE="${HERMES_HOME}/.cf_x402_proxy_key"
if [[ "${RADIUS_INFERENCE_PROVIDER:-}" == "cloudflare-x402" ]]; then
  if [[ ! -s "$CF_X402_PROXY_KEY_FILE" ]]; then
    (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(32))' > "$CF_X402_PROXY_KEY_FILE")
  fi
  export CF_X402_PROXY_KEY="$(cat "$CF_X402_PROXY_KEY_FILE")"
  export CF_X402_PORT
fi
echo "[bootstrap] Applying inference provider (${RADIUS_INFERENCE_PROVIDER:-default}) to config.yaml..."
python3 - <<'PYEOF'
import json
import os
import sys
from pathlib import Path

import yaml

home = Path(os.environ["HERMES_HOME"])
cfg_file = home / "config.yaml"
state_file = home / ".inference-provider-managed.json"
selected = os.environ.get("RADIUS_INFERENCE_PROVIDER", "").strip() == "cloudflare-x402"
base_url = f"http://127.0.0.1:{os.environ.get('CF_X402_PORT', '8650')}/v1"

try:
    cfg = yaml.safe_load(cfg_file.read_text()) or {}
except FileNotFoundError:
    cfg = {}
except Exception as exc:
    # Never rewrite a config.yaml that could not be read; Hermes reports it on start.
    print(f"[bootstrap] ERROR: could not read config.yaml ({exc}); inference provider not applied.", file=sys.stderr)
    sys.exit(1 if selected else 0)
if not isinstance(cfg, dict):
    print("[bootstrap] ERROR: config.yaml is not a mapping; inference provider not applied.", file=sys.stderr)
    sys.exit(1 if selected else 0)
state = None
if state_file.exists():
    try:
        state = json.loads(state_file.read_text())
    except Exception:
        state = None


def ours(model):
    return isinstance(model, dict) and model.get("provider") == "custom" and model.get("base_url") == base_url


if selected:
    model = {
        "provider": "custom",
        "default": os.environ.get("CF_X402_MODEL", "").strip() or "z-ai/glm-4.7-flash",
        "base_url": base_url,
        "api_key": os.environ["CF_X402_PROXY_KEY"],
    }
    ctx = os.environ.get("CF_X402_CONTEXT_LENGTH", "").strip()
    if ctx.isdigit():
        model["context_length"] = int(ctx)
    if state is None:
        # Remember what to restore on switch-back (the operator's provider, e.g. an OpenRouter model id).
        previous = cfg.get("model")
        state = {"provider": "cloudflare-x402", "previous_model": None if ours(previous) else previous}
        state_file.write_text(json.dumps(state))
    if cfg.get("model") != model:
        cfg["model"] = model
        cfg_file.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False))
    print(f"[bootstrap] Hermes main model -> cloudflare-x402 proxy ({model['default']} at {base_url}).")
elif state is not None:
    if ours(cfg.get("model")):
        llm_model = os.environ.get("LLM_MODEL", "").strip()
        restored = llm_model or state.get("previous_model") or "openai/gpt-5.4-nano"
        cfg["model"] = restored
        cfg_file.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False))
        print(f"[bootstrap] Hermes main model restored from cloudflare-x402 to {restored if isinstance(restored, str) else 'the previous model block'}.")
    state_file.unlink()
PYEOF

if [[ ! -f "$INIT_MARKER" ]]; then
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$INIT_MARKER"
  echo "[bootstrap] First-time initialization completed."
else
  echo "[bootstrap] Existing Hermes data found. Skipping one-time init."
fi

# === Radius CLI: wallet setup ===
RADIUS_WALLET_MARKER="${RADIUS_HOME}/initialized"
radius_cli_cmd=("${RADIUS_CLI_BIN:-radius-cli}")
if ! command -v "${radius_cli_cmd[0]}" >/dev/null 2>&1; then
  if command -v npx >/dev/null 2>&1; then
    radius_cli_cmd=(npx --yes radius-cli)
  fi
fi

# Export the keystore's private key to power JWT auth + ERC-8004 signing.
radius_export_private_key() {
  printf 'y\n' | "${radius_cli_cmd[@]}" wallet export 2>/dev/null \
    | grep -Eo '0x[a-fA-F0-9]{64}' | tail -n1 || true
}

# Signed faucet drip (challenge -> `radius-cli wallet sign` -> drip), as in the
# dripping-faucet skill. The key never leaves radius-cli for this step.
radius_faucet_drip() {
  local address="$1"
  local faucet_url="${RADIUS_FAUCET_URL:-https://testnet.radiustech.xyz/api/v1/faucet}"
  local challenge message signature body response status tx_hash error_code retry_ms
  faucet_url="${faucet_url%/}"

  challenge="$(curl -sS --max-time 15 "${faucet_url}/challenge/${address}?token=SBC")" || {
    echo "[radius] Faucet challenge request failed." >&2
    return 1
  }
  message="$(jq -r '.message // .challenge // empty' <<<"$challenge" 2>/dev/null || true)"
  if [[ -z "$message" ]]; then
    echo "[radius] Faucet returned no challenge message." >&2
    return 1
  fi

  signature="$(printf '%s' "$message" | "${radius_cli_cmd[@]}" wallet sign - 2>/dev/null \
    | grep -Eo '0x[a-fA-F0-9]{130}' | head -n1 || true)"
  if [[ -z "$signature" ]]; then
    echo "[radius] Could not sign the faucet challenge with radius-cli." >&2
    return 1
  fi

  body="$(jq -nc --arg address "$address" --arg signature "$signature" \
    '{address: $address, token: "SBC", signature: $signature}')"
  response="$(curl -sS --max-time 15 -X POST -H 'content-type: application/json' \
    -d "$body" -w '\n%{http_code}' "${faucet_url}/drip")" || {
    echo "[radius] Faucet drip request failed." >&2
    return 1
  }
  status="${response##*$'\n'}"
  response="${response%$'\n'*}"

  if [[ "$status" == 2* ]]; then
    tx_hash="$(jq -r '.tx_hash // .txHash // .hash // empty' <<<"$response" 2>/dev/null || true)"
    if [[ -n "$tx_hash" ]]; then
      echo "[radius] Faucet tx: ${tx_hash}"
    fi
    return 0
  fi

  error_code="$(jq -r 'if (.error | type) == "object" then .error.code else (.error // empty) end' <<<"$response" 2>/dev/null || true)"
  if [[ "$error_code" == "rate_limited" ]]; then
    retry_ms="$(jq -r '((.error | objects | .retry_after_ms) // .retry_after_ms // ((.retry_after_seconds // 0) * 1000)) | floor' <<<"$response" 2>/dev/null || echo 0)"
    echo "[radius] Faucet rate-limited. Retry after $(( ${retry_ms:-0} / 1000 ))s."
    return 0
  fi
  echo "[radius] Faucet drip failed (HTTP ${status}): ${error_code:-$response}" >&2
  return 1
}

radius_wait_for_sbc() {
  local address="$1" sbc
  for _ in 1 2 3 4 5; do
    sleep 3
    sbc="$("${radius_cli_cmd[@]}" --json wallet balance "$address" 2>/dev/null | jq -r '.sbc // empty' 2>/dev/null || true)"
    if [[ -n "$sbc" && "$sbc" != "0" ]]; then
      echo "[radius] SBC balance: ${sbc} SBC"
      return 0
    fi
  done
}

if [[ ! -f "$RADIUS_WALLET_MARKER" ]]; then
  echo "[bootstrap] Setting up Radius wallet via radius-cli..."

  if "${radius_cli_cmd[@]}" wallet address >/tmp/radius_wallet_address.txt 2>&1; then
    radius_addr="$(grep -Eo '0x[a-fA-F0-9]{40}' /tmp/radius_wallet_address.txt | head -n1 || true)"
    if [[ -n "$radius_addr" ]]; then
      echo "$radius_addr" > "$RADIUS_ADDR_FILE"
      export RADIUS_WALLET_ADDRESS="$radius_addr"
      if ! grep -q "^RADIUS_WALLET_ADDRESS=" "$ENV_FILE" 2>/dev/null; then
        echo "RADIUS_WALLET_ADDRESS=${RADIUS_WALLET_ADDRESS}" >> "$ENV_FILE"
      fi
    fi

    radius_key="$(radius_export_private_key)"
    if [[ -n "$radius_key" ]]; then
      export RADIUS_PRIVATE_KEY="$radius_key"
      if ! grep -q "^RADIUS_PRIVATE_KEY=" "$ENV_FILE" 2>/dev/null; then
        echo "RADIUS_PRIVATE_KEY=${RADIUS_PRIVATE_KEY}" >> "$ENV_FILE"
      fi
    fi

    # Optional faucet funding for first boot.
    if [[ "${RADIUS_AUTO_FUND:-true}" != "false" && "${RADIUS_AUTO_FUND:-true}" != "0" && -n "${radius_addr:-}" ]]; then
      echo "[radius] Requesting SBC from faucet..."
      if radius_faucet_drip "$radius_addr"; then
        radius_wait_for_sbc "$radius_addr" || true
      else
        echo "[radius] WARNING: faucet funding failed; continuing without it." >&2
      fi
    fi

    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$RADIUS_WALLET_MARKER"
    echo "[bootstrap] Radius wallet ready: ${RADIUS_WALLET_ADDRESS:-unknown}"
  else
    echo "[bootstrap] WARNING: Radius wallet setup failed. Will retry on next boot." >&2
    cat /tmp/radius_wallet_address.txt >&2 || true
  fi
else
  if [[ -f "$RADIUS_ADDR_FILE" ]]; then
    export RADIUS_WALLET_ADDRESS="$(cat "$RADIUS_ADDR_FILE")"
  fi
  # Ensure auth key is available even on warm boots.
  if [[ -z "${RADIUS_PRIVATE_KEY:-}" ]]; then
    radius_key="$(radius_export_private_key)"
    if [[ -n "$radius_key" ]]; then
      export RADIUS_PRIVATE_KEY="$radius_key"
    fi
  fi
  echo "[bootstrap] Radius wallet already initialized: ${RADIUS_WALLET_ADDRESS:-unknown}"
fi

# === ByteRover: configure memory provider (one-time) ===
BYTEROVER_MARKER="${HERMES_HOME}/.byterover/initialized"
mkdir -p "${HERMES_HOME}/.byterover"
if [[ -n "${BYTEROVER_API_KEY:-}" ]] || is_true "${BYTEROVER_LOCAL:-}"; then
  if [[ ! -f "$BYTEROVER_MARKER" ]]; then
    if [[ -n "${BYTEROVER_API_KEY:-}" ]]; then
      # Cloud mode: authenticate with API key
      export BRV_API_KEY="${BYTEROVER_API_KEY}"
      echo "[bootstrap] Authenticating ByteRover CLI..."
      if brv login -k "$BRV_API_KEY" 2>/dev/null || brv login --api-key "$BRV_API_KEY" 2>/dev/null; then
        echo "[bootstrap] ByteRover authenticated."
      else
        echo "[bootstrap] WARNING: brv login failed — check BYTEROVER_API_KEY." >&2
      fi
    else
      echo "[bootstrap] ByteRover local mode — skipping cloud authentication."
    fi
    echo "[bootstrap] Configuring Hermes to use ByteRover memory provider..."
    if hermes config set memory.provider byterover; then
      date -u +"%Y-%m-%dT%H:%M:%SZ" > "$BYTEROVER_MARKER"
      echo "[bootstrap] ByteRover memory provider configured."
    else
      echo "[bootstrap] WARNING: Failed to configure ByteRover. Will retry on next boot." >&2
    fi
  else
    echo "[bootstrap] ByteRover already configured."
    if [[ -n "${BYTEROVER_API_KEY:-}" ]]; then
      # Re-authenticate on every boot (token may have expired)
      export BRV_API_KEY="${BYTEROVER_API_KEY}"
      brv login -k "$BRV_API_KEY" 2>/dev/null || brv login --api-key "$BRV_API_KEY" 2>/dev/null || true
    fi
  fi
else
  echo "[bootstrap] BYTEROVER_API_KEY not set and BYTEROVER_LOCAL not enabled — skipping ByteRover setup."
fi

# === bundled plugins: ensure plugins and their toolsets are enabled in config.yaml ===
echo "[bootstrap] Discovering bundled plugins and plugin toolsets..."
python3 - <<'PYEOF'
import os
from pathlib import Path

import yaml

cfg_file = Path(os.environ["HERMES_HOME"]) / "config.yaml"
plugins_root = Path(os.environ.get("BUNDLED_PLUGINS_SOURCE", "/app/plugins"))

try:
    with cfg_file.open() as f:
        cfg = yaml.safe_load(f) or {}
except Exception:
    cfg = {}


def discover_plugin_names(root: Path) -> list[str]:
    names: list[str] = []
    if not root.exists():
        return names
    for manifest_path in sorted(root.glob("*/plugin.yaml")):
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:
            manifest = {}
        name = str(manifest.get("name") or manifest_path.parent.name).strip()
        if name and name not in names:
            names.append(name)
    return names


bundled_plugins = discover_plugin_names(plugins_root)
if not bundled_plugins:
    print(f"[bootstrap] No bundled plugin manifests discovered under {plugins_root}.")
else:
    print(f"[bootstrap] Bundled plugin manifests discovered: {bundled_plugins}")

toolsets = cfg.get("toolsets") or []
if not isinstance(toolsets, list):
    toolsets = []

plugins_cfg = cfg.get("plugins") or {}
if not isinstance(plugins_cfg, dict):
    plugins_cfg = {}
enabled_plugins = plugins_cfg.get("enabled") or []
if not isinstance(enabled_plugins, list):
    enabled_plugins = []
disabled_plugins = plugins_cfg.get("disabled") or []
if not isinstance(disabled_plugins, list):
    disabled_plugins = []

added_toolsets: list[str] = []
if "all" not in toolsets:
    for plugin_name in bundled_plugins:
        if plugin_name not in toolsets:
            toolsets.append(plugin_name)
            added_toolsets.append(plugin_name)

added_plugins: list[str] = []
if "all" not in enabled_plugins and "*" not in enabled_plugins:
    for plugin_name in bundled_plugins:
        if plugin_name not in enabled_plugins:
            enabled_plugins.append(plugin_name)
            added_plugins.append(plugin_name)

bundled_plugin_set = set(bundled_plugins)
removed_disabled_plugins = [
    plugin_name for plugin_name in disabled_plugins if plugin_name in bundled_plugin_set
]
if removed_disabled_plugins:
    disabled_plugins = [
        plugin_name for plugin_name in disabled_plugins if plugin_name not in bundled_plugin_set
    ]

cfg["toolsets"] = toolsets
plugins_cfg["enabled"] = enabled_plugins
plugins_cfg["disabled"] = disabled_plugins
cfg["plugins"] = plugins_cfg

if added_toolsets or added_plugins or removed_disabled_plugins:
    with cfg_file.open("w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    if added_toolsets:
        print(f"[bootstrap] Enabled bundled plugin toolsets: {added_toolsets}")
    if added_plugins:
        print(f"[bootstrap] Enabled bundled plugins: {added_plugins}")
    if removed_disabled_plugins:
        print(f"[bootstrap] Removed bundled plugins from plugins.disabled: {removed_disabled_plugins}")
else:
    print("[bootstrap] Bundled plugins and toolsets already enabled.")
PYEOF

# === shared-workspace hardening: re-applied from env on every boot ===
# HERMES_DISABLED_TOOLSETS   comma list -> agent.disabled_toolsets (e.g. terminal,file,code_execution); "none" clears it
# HERMES_BACKGROUND_REVIEW   true|false -> auxiliary.background_review.enabled
# HERMES_MEMORY_NOTIFICATIONS off|on|verbose -> display.memory_notifications
if [[ -n "${HERMES_DISABLED_TOOLSETS+x}" || -n "${HERMES_BACKGROUND_REVIEW:-}" || -n "${HERMES_MEMORY_NOTIFICATIONS:-}" ]]; then
  echo "[bootstrap] Applying shared-workspace settings to config.yaml..."
  python3 - <<'PYEOF'
import os
from pathlib import Path

import yaml

cfg_file = Path(os.environ["HERMES_HOME"]) / "config.yaml"
try:
    with cfg_file.open() as f:
        cfg = yaml.safe_load(f) or {}
except Exception:
    cfg = {}

def section(name):
    value = cfg.get(name)
    if not isinstance(value, dict):
        value = {}
        cfg[name] = value
    return value

if "HERMES_DISABLED_TOOLSETS" in os.environ:
    # "none" (or empty) clears the list, re-enabling everything a previous boot disabled.
    disabled = [t.strip() for t in os.environ["HERMES_DISABLED_TOOLSETS"].split(",")
                if t.strip() and t.strip().lower() != "none"]
    section("agent")["disabled_toolsets"] = disabled
    print(f"[bootstrap] agent.disabled_toolsets = {disabled}")

review = os.environ.get("HERMES_BACKGROUND_REVIEW", "").strip().lower()
if review:
    aux = section("auxiliary")
    br = aux.get("background_review") if isinstance(aux.get("background_review"), dict) else {}
    br["enabled"] = review in {"1", "true", "yes", "on"}
    aux["background_review"] = br
    print(f"[bootstrap] auxiliary.background_review.enabled = {br['enabled']}")

notifications = os.environ.get("HERMES_MEMORY_NOTIFICATIONS", "").strip().lower()
if notifications:
    section("display")["memory_notifications"] = notifications
    print(f"[bootstrap] display.memory_notifications = {notifications}")

with cfg_file.open("w") as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
PYEOF
fi

# === Slack experience settings: re-applied from env on every boot ===
# Each SLACK_* var below writes one Hermes config.yaml key. An unset var leaves
# the key alone unless this block wrote it on an earlier boot, in which case the
# key is removed, so Railway Variables stay the source of truth. "none" clears a
# key. Mention, bot and reaction gating are not written here: Hermes reads
# SLACK_REQUIRE_MENTION, SLACK_STRICT_MENTION, SLACK_ALLOW_BOTS,
# SLACK_ALLOWED_CHANNELS and SLACK_REACTION_TRIGGERS from the environment.
echo "[bootstrap] Applying Slack experience settings to config.yaml..."
python3 - <<'PYEOF' || echo "[bootstrap] WARNING: Could not apply Slack experience settings; continuing." >&2
import json
import os
import sys
from pathlib import Path

import yaml

home = Path(os.environ["HERMES_HOME"])
cfg_file = home / "config.yaml"
state_file = home / ".slack-experience-managed.json"

TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off"}
FOOTER_FIELDS = ("model", "context_pct", "latency", "served_model", "cwd")
DEFAULT_SUGGESTED_PROMPTS = [
    {"title": "Check my wallet", "message": "What is my Radius Testnet wallet address and SBC balance?"},
    {"title": "Try an x402 API", "message": "Show me three Radius Testnet x402 demo APIs and quote one without paying."},
    {"title": "What can you do?", "message": "What Radius tools and skills do you have, and when should I use each one?"},
    {"title": "Build on Radius", "message": "How do I send a stablecoin payment on Radius Testnet with viem?"},
]


def warn(message):
    print(f"[bootstrap] WARNING: {message}", file=sys.stderr)


def parse_bool(raw):
    value = raw.lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise ValueError("expected true or false")


def parse_live_status(raw):
    value = raw.lower()
    if value not in {"full", "verb", "off"}:
        raise ValueError("expected full, verb or off")
    return value


def parse_footer(raw):
    value = raw.lower()
    if value in FALSE:
        return {"enabled": False}
    if value in TRUE:
        # Leave out cwd: it would show the workspace path in shared channels.
        return {"enabled": True, "fields": ["model", "context_pct"]}
    fields = [f.strip() for f in value.split(",") if f.strip()]
    unknown = [f for f in fields if f not in FOOTER_FIELDS]
    if unknown:
        raise ValueError(f"unknown fields {unknown}; use {', '.join(FOOTER_FIELDS)}")
    return {"enabled": True, "fields": fields}


def parse_prompts(raw):
    if raw.lower() == "default":
        return DEFAULT_SUGGESTED_PROMPTS
    value = json.loads(raw)
    rows = value.get("prompts") if isinstance(value, dict) else value
    if not isinstance(rows, list) or not all(
        isinstance(r, dict) and r.get("title") and r.get("message") for r in rows
    ):
        raise ValueError("expected a JSON list of {title, message} or {title, prompts}")
    if len(rows) > 4:
        warn("SLACK_SUGGESTED_PROMPTS has more than 4 prompts; Slack shows the first 4.")
    return value


def parse_channel_prompts(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise ValueError("expected a JSON object of channel ID to prompt text")
    return {str(k): v for k, v in value.items()}


def parse_skill_bindings(raw):
    value = json.loads(raw)
    def valid_skills(entry):
        skills = entry.get("skills") or entry.get("skill")
        if isinstance(skills, str):
            return bool(skills.strip())
        return isinstance(skills, list) and bool(skills) and all(isinstance(s, str) for s in skills)

    if not isinstance(value, list) or not all(
        isinstance(e, dict) and e.get("id") and valid_skills(e) for e in value
    ):
        raise ValueError("expected a JSON list of {id, skills} or {id, skill}")
    return value


def describe(env_name, value):
    # Never log prompt text: print shapes, counts and channel IDs only.
    if env_name == "SLACK_CHANNEL_PROMPTS":
        return f"prompts for channels {sorted(value)}"
    if env_name == "SLACK_CHANNEL_SKILL_BINDINGS":
        return f"bindings for channels {[str(e['id']) for e in value]}"
    if env_name == "SLACK_SUGGESTED_PROMPTS":
        rows = value.get("prompts") if isinstance(value, dict) else value
        return f"{len(rows)} prompts"
    return value


SLACK_DISPLAY = ("display", "platforms", "slack")
SETTINGS = (
    ("SLACK_STREAMING", SLACK_DISPLAY + ("streaming",), parse_bool),
    ("SLACK_LIVE_STATUS", SLACK_DISPLAY + ("live_status",), parse_live_status),
    ("SLACK_RUNTIME_FOOTER", SLACK_DISPLAY + ("runtime_footer",), parse_footer),
    ("SLACK_RICH_BLOCKS", ("slack", "rich_blocks"), parse_bool),
    ("SLACK_NATIVE_TASK_CARDS", ("slack", "native_task_cards"), parse_bool),
    ("SLACK_FEEDBACK_BUTTONS", ("slack", "feedback_buttons"), parse_bool),
    ("SLACK_SUGGESTED_PROMPTS", ("slack", "suggested_prompts"), parse_prompts),
    ("SLACK_CHANNEL_PROMPTS", ("slack", "channel_prompts"), parse_channel_prompts),
    ("SLACK_CHANNEL_SKILL_BINDINGS", ("slack", "channel_skill_bindings"), parse_skill_bindings),
)


def set_path(cfg, path, value):
    node = cfg
    for key in path[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[path[-1]] = value


def get_path(cfg, path):
    node = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def remove_path(cfg, path):
    parents = [cfg]
    for key in path[:-1]:
        node = parents[-1].get(key)
        if not isinstance(node, dict):
            return
        parents.append(node)
    parents[-1].pop(path[-1], None)
    # Drop the parent sections this left empty.
    for depth in range(len(path) - 2, -1, -1):
        if parents[depth + 1]:
            break
        parents[depth].pop(path[depth], None)


try:
    with cfg_file.open() as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    cfg = {}
except Exception as exc:
    # Never rewrite a config we could not read: it would drop every other setting.
    warn(f"Could not parse {cfg_file} ({exc}); Slack experience settings not applied.")
    sys.exit(0)
if not isinstance(cfg, dict):
    warn(f"{cfg_file} is not a mapping; Slack experience settings not applied.")
    sys.exit(0)
try:
    managed = set(json.loads(state_file.read_text(encoding="utf-8")))
except Exception:
    managed = set()

original = json.dumps(cfg, sort_keys=True, default=str)
for env_name, path, parse in SETTINGS:
    raw = os.environ.get(env_name, "").strip()
    dotted = ".".join(path)
    if not raw:
        if env_name in managed:
            remove_path(cfg, path)
            managed.discard(env_name)
            print(f"[bootstrap] {dotted} removed ({env_name} is unset)")
        continue
    if raw.lower() == "none":
        remove_path(cfg, path)
        managed.discard(env_name)
        print(f"[bootstrap] {dotted} cleared ({env_name}=none)")
        continue
    try:
        value = parse(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        warn(f"Ignoring {env_name}: {exc}. {dotted} left unchanged.")
        continue
    set_path(cfg, path, value)
    managed.add(env_name)
    print(f"[bootstrap] {dotted} = {describe(env_name, value)}")

slack_cfg = cfg.get("slack") if isinstance(cfg.get("slack"), dict) else {}
slack_extra = get_path(cfg, ("platforms", "slack", "extra")) or {}


def slack_flag(key):
    raw = slack_cfg.get(key, slack_extra.get(key) if isinstance(slack_extra, dict) else None)
    return str(raw).strip().lower() in TRUE


# Hermes v2026.9.24 posts a natively streamed Slack reply a second time when
# the final text isn't an exact continuation of the stream, and never closes
# the stream, so "is working" stays up. Fixed upstream in Hermes v0.22.0.
# Hermes skips native streaming when an unfurl key is set.
unfurl_set = any(
    isinstance(section, dict) and section.get(key) is not None
    for section in (slack_cfg, slack_extra)
    for key in ("unfurl_links", "unfurl_media")
)
if get_path(cfg, SLACK_DISPLAY + ("streaming",)) is True and not unfurl_set:
    warn("Slack streaming in this Hermes version can post each reply twice and leave an "
         "'is working' indicator; set SLACK_STREAMING=false until Hermes is upgraded to v0.22.0 or later.")
if slack_flag("feedback_buttons") and not (slack_flag("rich_blocks") or slack_flag("markdown_blocks")):
    warn("Slack feedback buttons need Block Kit replies; set SLACK_RICH_BLOCKS=true or they are not shown.")
if slack_flag("native_task_cards"):
    # Same precedence as Hermes: per-platform, legacy override, global, then env.
    progress = next(
        (v for v in (
            get_path(cfg, SLACK_DISPLAY + ("tool_progress",)),
            get_path(cfg, ("display", "tool_progress_overrides", "slack")),
            get_path(cfg, ("display", "tool_progress")),
            os.environ.get("HERMES_TOOL_PROGRESS_MODE") or None,
        ) if v is not None),
        None,
    )
    if progress is not None and str(progress).strip().lower() in {"off", "false"}:
        warn("Slack task cards are off while tool progress is explicitly off; unset HERMES_TOOL_PROGRESS_MODE / display.tool_progress or use new/all.")

# Hermes' built-in Slack prompt hint says "tables are NOT supported" whether or
# not rich_blocks is on. When it is, append the formatting the renderer really
# supports. Any platform_hints.slack this block didn't write (or that an
# operator has since edited) is kept.
HINT_KEY = "platform_hints.slack"
HINT_PATH = ("platform_hints", "slack")
HINT_PREFIX = "Block Kit replies are on for this agent:"
BLOCKS_HINT = (
    HINT_PREFIX + " your markdown is rendered as Slack Block Kit, so "
    "#/## headers, - and 1. lists (nesting allowed), one pipe table per reply (up to 5 columns), "
    "--- dividers and > quotes display natively. This replaces the note above that tables are "
    "not supported. Keep a reply under about 20 blocks (each header, paragraph, list, table, "
    "divider or quote is one block); past Slack's 50-block limit the whole reply falls back to "
    "plain text. You write markdown only: you cannot send Block Kit JSON, blocks or attachments "
    "payloads, colored sidebars or custom buttons, and a JSON payload in a reply shows up as a "
    "code block. For a card or themed layout, use the slack-card skill."
)
# markdown_blocks (Slack's native markdown block) takes precedence in Hermes and
# has different limits, so the note is only for the rich_blocks renderer.
blocks_on = slack_flag("rich_blocks") and not slack_flag("markdown_blocks")
current_hint = get_path(cfg, HINT_PATH)
hint_managed = (
    HINT_KEY in managed
    and isinstance(current_hint, dict)
    and set(current_hint) == {"append"}
    and str(current_hint["append"]).startswith(HINT_PREFIX)
)
if not hint_managed:
    managed.discard(HINT_KEY)
if hint_managed or current_hint is None:
    if blocks_on:
        set_path(cfg, HINT_PATH, {"append": BLOCKS_HINT})
        managed.add(HINT_KEY)
        if not hint_managed:
            print(f"[bootstrap] {HINT_KEY} = Block Kit formatting note")
    elif hint_managed:
        remove_path(cfg, HINT_PATH)
        managed.discard(HINT_KEY)
        print(f"[bootstrap] {HINT_KEY} removed (Block Kit replies are off)")
elif blocks_on:
    print(f"[bootstrap] Keeping the existing {HINT_KEY}; it should say that tables render in Slack.")

if json.dumps(cfg, sort_keys=True, default=str) != original:
    tmp_file = cfg_file.with_name(cfg_file.name + ".slack-tmp")
    with tmp_file.open("w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    os.replace(tmp_file, cfg_file)
state_file.write_text(json.dumps(sorted(managed)), encoding="utf-8")
PYEOF

# === vendored skills: persist Radius external directory and discover skill roots ===
RADIUS_SKILLS_DIR="${RADIUS_SKILLS_DIR:-/data/.hermes/external-skills/radius-skills}"
RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE="${RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE:-true}"
radius_skills_snapshot=/app/vendor/radius-skills
mkdir -p "$(dirname "${RADIUS_SKILLS_DIR}")"

# --- radius skills bootstrap begin ---
# Radius skills moved from radiustechsystems/skills (archived) to
# radiustechsystems/tools. Replace a persisted checkout of the archived repo with
# the image snapshot; checkouts of any other repository are left alone.
if [[ -f "${RADIUS_SKILLS_DIR}/.git/HEAD" ]] && is_true "${RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE}" && [[ -d "${radius_skills_snapshot}" ]]; then
  radius_skills_origin="$(git -C "${RADIUS_SKILLS_DIR}" remote get-url origin 2>/dev/null || true)"
  radius_skills_origin="${radius_skills_origin%/}"
  if [[ "${radius_skills_origin%.git}" == "https://github.com/radiustechsystems/skills" ]]; then
    echo "[bootstrap] Replacing the archived radiustechsystems/skills checkout in ${RADIUS_SKILLS_DIR} with the image snapshot."
    rm -rf "${RADIUS_SKILLS_DIR}"
  fi
fi

if [[ ! -f "${RADIUS_SKILLS_DIR}/.git/HEAD" ]]; then
  if is_true "${RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE}" && [[ -d "${radius_skills_snapshot}" ]]; then
    echo "[bootstrap] Bootstrapping Radius external skills from image snapshot into ${RADIUS_SKILLS_DIR}..."
    rm -rf "${RADIUS_SKILLS_DIR}"
    cp -a "${radius_skills_snapshot}" "${RADIUS_SKILLS_DIR}"
  else
    echo "[bootstrap] Radius external skills directory missing and bootstrap disabled; creating empty directory at ${RADIUS_SKILLS_DIR}."
    mkdir -p "${RADIUS_SKILLS_DIR}"
  fi
fi
# --- radius skills bootstrap end ---

VENDORED_SKILLS_SOURCE="${VENDORED_SKILLS_SOURCE:-${RADIUS_SKILLS_DIR}}"
VENDORED_SKILLS_MANIFEST="${HERMES_HOME}/vendored-skills.json"
export VENDORED_SKILLS_SOURCE VENDORED_SKILLS_MANIFEST

echo "[bootstrap] Discovering vendored skills under ${VENDORED_SKILLS_SOURCE}..."
python3 - <<'PYEOF'
import json
import os
from pathlib import Path

source = Path(os.environ["VENDORED_SKILLS_SOURCE"])
manifest_path = Path(os.environ["VENDORED_SKILLS_MANIFEST"])

skills = []
roots = set()

if source.exists():
    for skill_md in sorted(source.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        skill_name = skill_dir.name
        root = str(skill_dir.parent)
        published = False
        try:
            content = skill_md.read_text(encoding="utf-8")
            published = "published: true" in content
        except Exception:
            pass
        skills.append(
            {
                "name": skill_name,
                "path": str(skill_dir),
                "root": root,
                "published": published,
            }
        )
        roots.add(root)

manifest = {
    "source": str(source),
    "roots": sorted(roots),
    "skills": skills,
}
manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print(f"[bootstrap] Vendored skill roots discovered: {manifest['roots']}")
print(f"[bootstrap] Vendored skills discovered: {[skill['name'] for skill in skills]}")
PYEOF

EXPECTED_VENDORED_SKILLS="${EXPECTED_VENDORED_SKILLS:-}"
STRICT_VENDORED_SKILLS="${STRICT_VENDORED_SKILLS:-false}"
if [[ -n "$EXPECTED_VENDORED_SKILLS" ]]; then
  export EXPECTED_VENDORED_SKILLS STRICT_VENDORED_SKILLS
  python3 - <<'PYEOF'
import json
import os
import sys
from pathlib import Path

manifest = json.loads(Path(os.environ["VENDORED_SKILLS_MANIFEST"]).read_text(encoding="utf-8"))
expected = [item.strip() for item in os.environ.get("EXPECTED_VENDORED_SKILLS", "").split(",") if item.strip()]
discovered = {skill["name"] for skill in manifest.get("skills", [])}
missing = [name for name in expected if name not in discovered]
if missing:
    message = f"[bootstrap] WARNING: Expected vendored skills not found: {missing}. Discovered: {sorted(discovered)}"
    if os.environ.get("STRICT_VENDORED_SKILLS", "").lower() in {"1", "true", "yes", "on"}:
        print(message, file=sys.stderr)
        sys.exit(1)
    print(message, file=sys.stderr)
PYEOF
fi

# === external skill directories: register discovered vendored skill roots in config.yaml ===
echo "[bootstrap] Registering vendored Radius skill directories as Hermes external skill dirs..."
python3 - <<'PYEOF'
import json
import os
import yaml
from pathlib import Path

cfg_file = os.environ['HERMES_HOME'] + '/config.yaml'
manifest = json.loads(Path(os.environ["VENDORED_SKILLS_MANIFEST"]).read_text(encoding="utf-8"))
roots = manifest.get("roots", [])
vendored_source = os.environ["VENDORED_SKILLS_SOURCE"]
legacy_vendored_source = "/app/vendor/radius-skills"

try:
    with open(cfg_file) as f:
        cfg = yaml.safe_load(f) or {}
except Exception:
    cfg = {}

skills_cfg = cfg.get('skills') or {}
external_dirs = skills_cfg.get('external_dirs') or []
filtered = [
    path
    for path in external_dirs
    if not str(path).startswith(vendored_source) and not str(path).startswith(legacy_vendored_source)
]
merged = filtered[:]
for root in roots:
    if root not in merged:
        merged.append(root)

if merged != external_dirs:
    skills_cfg['external_dirs'] = merged
    cfg['skills'] = skills_cfg
    with open(cfg_file, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    print('[bootstrap] Vendored external skill dirs registered.')
else:
    print('[bootstrap] Vendored external skill dirs already registered.')
PYEOF

# Install bundled skills into Hermes skills directory
SKILLS_DIR="${HERMES_HOME}/skills"
mkdir -p "$SKILLS_DIR"
for skill_file in /app/skills/*.md; do
  [[ -f "$skill_file" ]] || continue
  skill_basename="$(basename "$skill_file")"
  skill_name="${skill_basename%.md}"
  cp "$skill_file" "${SKILLS_DIR}/${skill_basename}"
  skill_target_dir="${SKILLS_DIR}/${skill_name}"
  mkdir -p "$skill_target_dir"
  cp "$skill_file" "${skill_target_dir}/SKILL.md"

  case "$skill_name" in
    radius-wallet|a2a-comms|registering-agent)
      target_dir="${SKILLS_DIR}/radius/${skill_name}"
      mkdir -p "$target_dir"
      cp "$skill_file" "${target_dir}/SKILL.md"
      echo "[bootstrap] Installed skill: ${skill_basename} (category: radius, directory layout)"
      ;;
    *)
      echo "[bootstrap] Installed skill: ${skill_basename} (directory layout)"
      ;;
  esac
done

# Vendored Radius marketplace skills are exposed to Hermes via
# `skills.external_dirs` in config.yaml instead of being copied into the
# primary Hermes skills directory.
python3 - <<'PYEOF'
import json
import os
import shutil
from pathlib import Path

manifest = json.loads(Path(os.environ["VENDORED_SKILLS_MANIFEST"]).read_text(encoding="utf-8"))
skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"

# Cleanup for older template revisions that copied vendored skills into
# ${HERMES_HOME}/skills/radius/<skill>/SKILL.md. Vendored skills should now be
# resolved only via skills.external_dirs in config.yaml.
built_in_radius_skills = {"radius-wallet", "a2a-comms", "registering-agent"}
legacy_radius_root = skills_dir / "radius"
for skill in manifest.get("skills", []):
    skill_name = skill.get("name")
    if not skill_name or skill_name in built_in_radius_skills:
        continue
    legacy_skill_dir = legacy_radius_root / skill_name
    if legacy_skill_dir.exists():
        shutil.rmtree(legacy_skill_dir, ignore_errors=True)
        print(f"[bootstrap] Removed legacy local vendored skill copy: {legacy_skill_dir}")

for skills_root in manifest.get("roots", []):
    print(f"[bootstrap] External skill directory available: {skills_root}")
PYEOF

# Install bundled plugins into Hermes plugins directory
PLUGINS_DIR="${HERMES_HOME}/plugins"
mkdir -p "$PLUGINS_DIR"
for plugin_dir in /app/plugins/*/; do
  [[ -d "$plugin_dir" ]] || continue
  plugin_name="$(basename "$plugin_dir")"
  rm -rf "${PLUGINS_DIR}/${plugin_name}"
  cp -r "$plugin_dir" "${PLUGINS_DIR}/${plugin_name}"
  echo "[bootstrap] Installed plugin: ${plugin_name}"
done

# GoDaddy surfaces have two independent paths: the remote GoDaddy MCP domain
# server and the local godaddy-ans plugin. Verify the local plugin can register
# tools before Hermes gateway starts so missing manifests/imports are visible in
# deployment logs instead of only as model-side terminal fallbacks.
STRICT_GODADDY_RUNTIME="${STRICT_GODADDY_RUNTIME:-false}"
export STRICT_GODADDY_RUNTIME
python3 - <<'PYEOF'
import importlib.util
import os
import sys
from pathlib import Path

import yaml


class _ToolCtx:
    def __init__(self) -> None:
        self.tools = []
        self.hooks = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_hook(self, name, callback) -> None:
        self.hooks.append((name, callback))


def _is_strict() -> bool:
    return os.environ.get("STRICT_GODADDY_RUNTIME", "").lower() in {"1", "true", "yes", "on"}


cfg_file = Path(os.environ["HERMES_HOME"]) / "config.yaml"
plugins_dir = Path(os.environ["HERMES_HOME"]) / "plugins"
plugin_dir = plugins_dir / "godaddy-ans"
errors = []
warnings = []

try:
    cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
except Exception as exc:
    cfg = {}
    warnings.append(f"could not read config.yaml: {exc}")

mcp_servers = cfg.get("mcp_servers") or {}
if isinstance(mcp_servers, dict) and "godaddy" not in mcp_servers:
    warnings.append("mcp_servers.godaddy is not configured; GoDaddy domain MCP tools may be unavailable.")

plugins_cfg = cfg.get("plugins") or {}
enabled_plugins = plugins_cfg.get("enabled") or []
disabled_plugins = plugins_cfg.get("disabled") or []
if isinstance(enabled_plugins, list):
    if "all" not in enabled_plugins and "*" not in enabled_plugins and "godaddy-ans" not in enabled_plugins:
        errors.append("plugins.enabled does not include godaddy-ans.")
if isinstance(disabled_plugins, list) and "godaddy-ans" in disabled_plugins:
    errors.append("plugins.disabled still includes godaddy-ans.")

manifest_path = plugin_dir / "plugin.yaml"
module_path = plugin_dir / "__init__.py"
if not manifest_path.exists():
    errors.append(f"missing GoDaddy ANS plugin manifest: {manifest_path}")
if not module_path.exists():
    errors.append(f"missing GoDaddy ANS plugin module: {module_path}")

expected_tools = {
    "godaddy_ans_capabilities",
    "godaddy_ans_prepare_registration",
    "godaddy_ans_register",
    "godaddy_ans_search",
    "godaddy_ans_get_agent",
    "godaddy_ans_resolve",
    "godaddy_ans_revoke",
    "godaddy_ans_verify_acme",
    "godaddy_ans_verify_dns",
    "godaddy_dns_set_records",
    "godaddy_ans_get_identity_certificates",
    "godaddy_ans_submit_identity_csr",
    "godaddy_ans_get_server_certificates",
    "godaddy_ans_submit_server_csr",
    "godaddy_ans_get_csr_status",
    "godaddy_ans_events",
}

if module_path.exists():
    try:
        spec = importlib.util.spec_from_file_location("godaddy_ans_runtime_check", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        ctx = _ToolCtx()
        module.register(ctx)
        registered = {tool.get("name") for tool in ctx.tools}
        missing = sorted(expected_tools - registered)
        if missing:
            errors.append(f"godaddy-ans did not register expected tools: {missing}")
        if "pre_llm_call" not in {name for name, _callback in ctx.hooks}:
            errors.append("godaddy-ans did not register its pre_llm_call routing hook.")
    except Exception as exc:
        errors.append(f"could not import/register godaddy-ans plugin: {exc}")

for warning in warnings:
    print(f"[bootstrap] WARNING: {warning}", file=sys.stderr)

if errors:
    message = "[bootstrap] GoDaddy MCP and ANS runtime surfaces check failed: " + "; ".join(errors)
    if _is_strict():
        print(message, file=sys.stderr)
        sys.exit(1)
    print(f"[bootstrap] WARNING: {message}", file=sys.stderr)
else:
    print("[bootstrap] GoDaddy MCP and ANS runtime surfaces verified.")
PYEOF

# Seed the messaging workspace so gateway sessions discover bundled project context
# from MESSAGING_CWD immediately.
link_into_workspace() {
  local src="$1"
  local dest="$2"
  local force="${3:-false}"

  if [[ ! -e "$src" ]]; then
    return 0
  fi

  if [[ "$force" == "true" ]]; then
    ln -sfn "$src" "$dest"
    echo "[bootstrap] Linked workspace asset: $(basename "$dest")"
  elif [[ -L "$dest" || ! -e "$dest" ]]; then
    ln -sfn "$src" "$dest"
    echo "[bootstrap] Linked workspace asset: $(basename "$dest")"
  else
    echo "[bootstrap] Keeping existing workspace asset: $(basename "$dest")" >&2
  fi
}

link_into_workspace /app/HERMES.md "${MESSAGING_CWD}/HERMES.md" true
link_into_workspace /app/HERMES.md "${MESSAGING_CWD}/.hermes.md" true
# Older images also linked a runtime AGENTS.md. Hermes loads only the first
# context file it finds and HERMES.md wins, so it was never read; drop the link.
if [[ "$(readlink "${MESSAGING_CWD}/AGENTS.md" 2>/dev/null)" == "/app/AGENTS.md" ]]; then
  rm -f "${MESSAGING_CWD}/AGENTS.md"
fi
link_into_workspace /app/README.md "${MESSAGING_CWD}/README.md"
link_into_workspace "$SKILLS_DIR" "${MESSAGING_CWD}/skills"
link_into_workspace "$PLUGINS_DIR" "${MESSAGING_CWD}/plugins"
link_into_workspace /app/scripts "${MESSAGING_CWD}/scripts"

# Populate .well-known skills directory — only skills with `published: true` in frontmatter
# Sources: bundled flat skills and vendored external skill directories
WELL_KNOWN_SKILLS_DIR="${HERMES_HOME}/well-known-skills"
rm -rf "$WELL_KNOWN_SKILLS_DIR"
mkdir -p "$WELL_KNOWN_SKILLS_DIR"
for skill_file in /app/skills/*.md; do
  [[ -f "$skill_file" ]] || continue
  skill_name="$(basename "$skill_file" .md)"
  grep -q "^published: true" "$skill_file" || continue
  mkdir -p "${WELL_KNOWN_SKILLS_DIR}/${skill_name}"
  cp "$skill_file" "${WELL_KNOWN_SKILLS_DIR}/${skill_name}/SKILL.md"
  echo "[bootstrap] Installed well-known skill: ${skill_name}"
done
python3 - <<'PYEOF'
import json
import os
import shutil
from pathlib import Path

manifest = json.loads(Path(os.environ["VENDORED_SKILLS_MANIFEST"]).read_text(encoding="utf-8"))
well_known_root = Path(os.environ["HERMES_HOME"]) / "well-known-skills"

for skill in manifest.get("skills", []):
    if not skill.get("published"):
        continue
    skill_name = skill["name"]
    skill_md = Path(skill["path"]) / "SKILL.md"
    target = well_known_root / skill_name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_md, target)
    print(f"[bootstrap] Installed vendored well-known skill: {skill_name}")
PYEOF

if [[ -z "${TELEGRAM_ALLOWED_USERS:-}${DISCORD_ALLOWED_USERS:-}${SLACK_ALLOWED_USERS:-}" ]]; then
  if ! is_true "${GATEWAY_ALLOW_ALL_USERS:-}" && ! is_true "${TELEGRAM_ALLOW_ALL_USERS:-}" && ! is_true "${DISCORD_ALLOW_ALL_USERS:-}" && ! is_true "${SLACK_ALLOW_ALL_USERS:-}"; then
    echo "[bootstrap] WARNING: No allowlists configured. Gateway defaults to deny-all; use DM pairing or set *_ALLOWED_USERS." >&2
  fi
fi

# Unset integer-typed env vars that are empty or whitespace-only to prevent
# int() parsing failures in hermes-agent (e.g. HERMES_MAX_ITERATIONS).
for key in \
  HERMES_MAX_ITERATIONS HERMES_NOUS_MIN_KEY_TTL_SECONDS \
  CONTEXT_COMPRESSION_THRESHOLD \
  TERMINAL_TIMEOUT TERMINAL_LIFETIME_SECONDS \
  TERMINAL_CONTAINER_CPU TERMINAL_CONTAINER_MEMORY TERMINAL_CONTAINER_DISK \
  TERMINAL_SSH_PORT BROWSER_SESSION_TIMEOUT BROWSER_INACTIVITY_TIMEOUT; do
  val="${!key:-}"
  if [[ -z "${val//[[:space:]]/}" ]]; then
    unset "$key"
  fi
done

# Hermes main can create idx_tasks_session_id before its additive migration
# adds tasks.session_id on legacy kanban DBs. Repair existing boards first.
echo "[bootstrap] Checking Hermes kanban SQLite migrations..."
python3 - <<'PYEOF' || echo "[bootstrap] WARNING: Hermes kanban migration preflight failed; gateway will continue." >&2
import os
import sqlite3
from pathlib import Path


def _candidate_paths() -> list[Path]:
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return [Path(override).expanduser()]

    root = Path(
        os.environ.get("HERMES_KANBAN_HOME")
        or os.environ.get("HERMES_HOME")
        or "/data/.hermes"
    ).expanduser()
    paths = [root / "kanban.db"]
    boards_root = root / "kanban" / "boards"
    if boards_root.is_dir():
        paths.extend(sorted(boards_root.glob("*/kanban.db")))
    return paths


seen: set[str] = set()
for db_path in _candidate_paths():
    resolved = str(db_path.resolve()) if db_path.exists() else str(db_path)
    if resolved in seen:
        continue
    seen.add(resolved)

    if not db_path.exists():
        continue

    conn = None
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "tasks" not in tables:
            continue

        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        changed = False
        if "session_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")
            changed = True
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_session_id "
            "ON tasks(session_id)"
        )
        if changed:
            print(f"[bootstrap] Repaired kanban DB migration: {db_path}")
    except sqlite3.DatabaseError as exc:
        print(
            f"[bootstrap] WARNING: Could not inspect kanban DB {db_path}: {exc}",
            flush=True,
        )
    finally:
        if conn is not None:
            conn.close()
PYEOF

# === cf_x402_proxy: local OpenAI-compatible endpoint paying Cloudflare per call (x402, Base USDC) ===
CF_X402_DIR="${CF_X402_DIR:-/app/scripts/cf_x402_proxy}"
if [[ "${RADIUS_INFERENCE_PROVIDER:-}" == "cloudflare-x402" ]]; then
  # Fail the boot on a bad configuration instead of starting an agent whose every turn fails.
  if ! node "${CF_X402_DIR}/proxy.mjs" --check; then
    echo "[bootstrap] ERROR: cloudflare-x402 proxy configuration is invalid (see above)." >&2
    exit 1
  fi
  echo "[bootstrap] Starting cloudflare-x402 proxy on 127.0.0.1:${CF_X402_PORT}..."
  (
    backoff=2
    while true; do
      node "${CF_X402_DIR}/proxy.mjs" && code=0 || code=$?
      if [[ "$code" -eq 2 ]]; then
        echo "[cf-x402] ERROR: configuration rejected; not restarting." >&2
        exit 2
      fi
      echo "[cf-x402] WARNING: proxy exited (${code}); restarting in ${backoff}s." >&2
      sleep "$backoff"
      backoff=$(( backoff < 60 ? backoff * 2 : 60 ))
    done
  ) &
  CF_X402_PID=$!
  cf_x402_ready=false
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${CF_X402_PORT}/health" >/dev/null 2>&1; then
      cf_x402_ready=true
      echo "[bootstrap] cloudflare-x402 proxy ready."
      break
    fi
    sleep 1
  done
  if [[ "$cf_x402_ready" != true ]]; then
    echo "[bootstrap] WARNING: cloudflare-x402 proxy is not answering on 127.0.0.1:${CF_X402_PORT}; model calls will fail until it does (see [cf-x402] lines)." >&2
  fi
  # Only the proxy needs these; keep them out of the agent's and gateway's environment.
  # (Advisory only: same-user processes can still read /proc/1/environ. The real caps are the
  # wallet balance, the bounded Permit2 allowance and the Cloudflare token's scope; see README.)
  unset CLOUDFLARE_API_TOKEN CF_X402_PRIVATE_KEY CF_X402_PROXY_KEY
fi

echo "[bootstrap] Starting agent server..."
python3 /app/scripts/agent_server/main.py &
AGENT_PID=$!

_wait_for_agent_server() {
  local port="${PORT:-3000}"
  local max_attempts=15
  local attempt=0

  echo "[bootstrap] Waiting for agent server on port ${port}..."
  while [[ $attempt -lt $max_attempts ]]; do
    attempt=$((attempt + 1))

    if ! kill -0 "$AGENT_PID" 2>/dev/null; then
      echo "[bootstrap] WARNING: Agent server process (PID ${AGENT_PID}) exited unexpectedly." >&2
      return 1
    fi

    local token
    token=$(python3 /app/scripts/agent_server/gen_jwt.py 2>/dev/null \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null)

    if [[ -n "$token" ]]; then
      local response status
      response=$(curl -sf -w "\n%{http_code}" \
        -H "Authorization: Bearer ${token}" \
        "http://localhost:${port}/health" 2>/dev/null)
      status="${response##*$'\n'}"
      local body="${response%$'\n'*}"
      if [[ "$status" == "200" ]]; then
        echo "[bootstrap] Agent server ready (attempt ${attempt}): ${body}"
        return 0
      fi
    fi

    sleep 2
  done

  echo "[bootstrap] WARNING: Agent server did not become ready after $((max_attempts * 2))s — continuing anyway." >&2
  return 1
}

_wait_for_agent_server || true

start_hermes_log_forwarders

echo "[bootstrap] Starting Hermes gateway..."
hermes gateway &
GATEWAY_PID=$!

trap 'cleanup $?' EXIT INT TERM

if ! wait -n "$AGENT_PID" "$GATEWAY_PID"; then
  status=$?
else
  status=0
fi

if ! kill -0 "$AGENT_PID" 2>/dev/null; then
  echo "[bootstrap] ERROR: Agent server exited." >&2
  status=1
fi

if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
  echo "[bootstrap] ERROR: Hermes gateway exited." >&2
  status=1
fi

cleanup "$status"
