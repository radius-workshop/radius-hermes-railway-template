#!/usr/bin/env bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/data/.hermes}"
export HOME="${HOME:-/data}"
export MESSAGING_CWD="${MESSAGING_CWD:-/data/workspace}"

INIT_MARKER="${HERMES_HOME}/.initialized"
ENV_FILE="${HERMES_HOME}/.env"
CONFIG_FILE="${HERMES_HOME}/config.yaml"

mkdir -p "${HERMES_HOME}" "${HERMES_HOME}/logs" "${HERMES_HOME}/sessions" "${HERMES_HOME}/cron" "${HERMES_HOME}/pairing" "${MESSAGING_CWD}"

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
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

if ! has_valid_provider_config; then
  echo "[bootstrap] ERROR: Configure a provider: OPENROUTER_API_KEY, or OPENAI_BASE_URL+OPENAI_API_KEY, or ANTHROPIC_API_KEY." >&2
  exit 1
fi

validate_platforms

# === Radius: pre-load stored wallet keys into environment ===
RADIUS_KEY_FILE="${HERMES_HOME}/.radius/key"
RADIUS_ADDR_FILE="${HERMES_HOME}/.radius/address"
mkdir -p "${HERMES_HOME}/.radius"
if [[ -z "${RADIUS_PRIVATE_KEY:-}" && -f "$RADIUS_KEY_FILE" ]]; then
  export RADIUS_PRIVATE_KEY="$(cat "$RADIUS_KEY_FILE")"
  echo "[bootstrap] Loaded stored Radius wallet key."
fi
if [[ -z "${RADIUS_WALLET_ADDRESS:-}" && -f "$RADIUS_ADDR_FILE" ]]; then
  export RADIUS_WALLET_ADDRESS="$(cat "$RADIUS_ADDR_FILE")"
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
  SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_ALLOW_ALL_USERS SLACK_HOME_CHANNEL SLACK_HOME_CHANNEL_NAME WHATSAPP_ENABLED WHATSAPP_ALLOWED_USERS \
  GATEWAY_ALLOW_ALL_USERS \
  FIRECRAWL_API_KEY NOUS_API_KEY BROWSERBASE_API_KEY BROWSERBASE_PROJECT_ID BROWSERBASE_PROXIES BROWSERBASE_ADVANCED_STEALTH BROWSER_SESSION_TIMEOUT BROWSER_INACTIVITY_TIMEOUT FAL_KEY ELEVENLABS_API_KEY VOICE_TOOLS_OPENAI_KEY \
  TINKER_API_KEY WANDB_API_KEY RL_API_URL GITHUB_TOKEN \
  TERMINAL_ENV TERMINAL_BACKEND TERMINAL_DOCKER_IMAGE TERMINAL_SINGULARITY_IMAGE TERMINAL_MODAL_IMAGE TERMINAL_CWD TERMINAL_TIMEOUT TERMINAL_LIFETIME_SECONDS TERMINAL_CONTAINER_CPU TERMINAL_CONTAINER_MEMORY TERMINAL_CONTAINER_DISK TERMINAL_CONTAINER_PERSISTENT TERMINAL_SANDBOX_DIR TERMINAL_SSH_HOST TERMINAL_SSH_USER TERMINAL_SSH_PORT TERMINAL_SSH_KEY SUDO_PASSWORD \
  WEB_TOOLS_DEBUG VISION_TOOLS_DEBUG MOA_TOOLS_DEBUG IMAGE_TOOLS_DEBUG CONTEXT_COMPRESSION_ENABLED CONTEXT_COMPRESSION_THRESHOLD CONTEXT_COMPRESSION_MODEL HERMES_MAX_ITERATIONS HERMES_TOOL_PROGRESS HERMES_TOOL_PROGRESS_MODE \
  RADIUS_PRIVATE_KEY RADIUS_WALLET_ADDRESS RADIUS_NETWORK RADIUS_AUTO_FUND
do
  append_if_set "$key"
done

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "[bootstrap] Creating ${CONFIG_FILE}"
  cat > "$CONFIG_FILE" <<EOF
model: ${LLM_MODEL:-anthropic/claude-3.5-haiku}
terminal:
  backend: ${TERMINAL_ENV:-${TERMINAL_BACKEND:-local}}
  cwd: ${TERMINAL_CWD:-/data/workspace}
  timeout: ${TERMINAL_TIMEOUT:-180}
compression:
  enabled: true
  threshold: 0.85
EOF
fi

# Ensure model is set in config.yaml (handles existing installs and model changes)
if [[ -n "${LLM_MODEL:-}" ]]; then
  if grep -q "^model:" "$CONFIG_FILE" 2>/dev/null; then
    sed -i "s|^model:.*|model: ${LLM_MODEL}|" "$CONFIG_FILE"
  else
    sed -i "1s|^|model: ${LLM_MODEL}\n|" "$CONFIG_FILE"
  fi
fi

if [[ ! -f "$INIT_MARKER" ]]; then
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$INIT_MARKER"
  echo "[bootstrap] First-time initialization completed."
else
  echo "[bootstrap] Existing Hermes data found. Skipping one-time init."
fi

# === Radius: wallet setup ===
RADIUS_WALLET_MARKER="${HERMES_HOME}/.radius/initialized"
if command -v node >/dev/null 2>&1; then
  if [[ ! -f "$RADIUS_WALLET_MARKER" ]]; then
    echo "[bootstrap] Setting up Radius wallet..."
    if node /app/scripts/radius/wallet-init.mjs; then
      date -u +"%Y-%m-%dT%H:%M:%SZ" > "$RADIUS_WALLET_MARKER"
      # Reload keys generated during init and append to .env for this boot
      if [[ -f "$RADIUS_KEY_FILE" ]] && ! grep -q "^RADIUS_PRIVATE_KEY=" "$ENV_FILE" 2>/dev/null; then
        echo "RADIUS_PRIVATE_KEY=$(cat "$RADIUS_KEY_FILE")" >> "$ENV_FILE"
      fi
      if [[ -f "$RADIUS_ADDR_FILE" ]] && ! grep -q "^RADIUS_WALLET_ADDRESS=" "$ENV_FILE" 2>/dev/null; then
        echo "RADIUS_WALLET_ADDRESS=$(cat "$RADIUS_ADDR_FILE")" >> "$ENV_FILE"
      fi
      echo "[bootstrap] Radius wallet ready: $(cat "$RADIUS_ADDR_FILE" 2>/dev/null || echo 'unknown')"
    else
      echo "[bootstrap] WARNING: Radius wallet setup failed. Will retry on next boot." >&2
    fi
  else
    echo "[bootstrap] Radius wallet already initialized: ${RADIUS_WALLET_ADDRESS:-unknown}"
  fi
else
  echo "[bootstrap] WARNING: Node.js not found, skipping Radius wallet setup." >&2
fi

# Install Radius skill into Hermes skills directory
SKILLS_DIR="${HERMES_HOME}/skills"
mkdir -p "$SKILLS_DIR"
for skill_file in /app/skills/*.md; do
  [[ -f "$skill_file" ]] || continue
  cp "$skill_file" "${SKILLS_DIR}/$(basename "$skill_file")"
  echo "[bootstrap] Installed skill: $(basename "$skill_file")"
done

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

echo "[bootstrap] Starting Hermes gateway..."
exec hermes gateway
