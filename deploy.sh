#!/usr/bin/env bash
# Pre-deploy env var check. Run this instead of `railway up`.
#
# Usage: ./deploy.sh [--reset-state] [extra railway up args]
#
set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RESET='\033[0m'

error()   { echo -e "${RED}[deploy] ERROR: $*${RESET}" >&2; }
warn()    { echo -e "${YELLOW}[deploy] WARNING: $*${RESET}" >&2; }
success() { echo -e "${GREEN}[deploy] $*${RESET}"; }

RESET_STATE=false
RAILWAY_UP_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset-state)
      RESET_STATE=true
      shift
      ;;
    *)
      RAILWAY_UP_ARGS+=("$1")
      shift
      ;;
  esac
done

# ── Require railway CLI ──────────────────────────────────────────────────────
if ! command -v railway &>/dev/null; then
  error "railway CLI not found. Install it: https://docs.railway.com/guides/cli"
  exit 1
fi

# ── Fetch current Railway variables ─────────────────────────────────────────
echo "[deploy] Fetching Railway environment variables..."
if ! vars_json=$(railway variables --json 2>/dev/null); then
  error "Could not fetch Railway variables. Make sure you're logged in and have run 'railway link'."
  exit 1
fi

get_var() { echo "$vars_json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null; }

# ── Check: AI provider ───────────────────────────────────────────────────────
OPENROUTER_API_KEY="$(get_var OPENROUTER_API_KEY)"
OPENAI_BASE_URL="$(get_var OPENAI_BASE_URL)"
OPENAI_API_KEY="$(get_var OPENAI_API_KEY)"
ANTHROPIC_API_KEY="$(get_var ANTHROPIC_API_KEY)"

provider_ok=false
if [[ -n "$OPENROUTER_API_KEY" ]]; then
  provider_ok=true
elif [[ -n "$OPENAI_BASE_URL" && -n "$OPENAI_API_KEY" ]]; then
  provider_ok=true
elif [[ -n "$ANTHROPIC_API_KEY" ]]; then
  provider_ok=true
fi

if ! $provider_ok; then
  error "No AI provider configured. Set one of:"
  echo "  • OPENROUTER_API_KEY"
  echo "  • OPENAI_BASE_URL + OPENAI_API_KEY"
  echo "  • ANTHROPIC_API_KEY"
  echo ""
  echo "  Add via: railway variables --set KEY=value"
  echo "  Or via the Railway dashboard → your service → Variables"
  MISSING=true
fi

# ── Check: messaging platform ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN="$(get_var TELEGRAM_BOT_TOKEN)"
DISCORD_BOT_TOKEN="$(get_var DISCORD_BOT_TOKEN)"
SLACK_BOT_TOKEN="$(get_var SLACK_BOT_TOKEN)"
SLACK_APP_TOKEN="$(get_var SLACK_APP_TOKEN)"

platform_ok=false
if [[ -n "$TELEGRAM_BOT_TOKEN" ]]; then
  platform_ok=true
elif [[ -n "$DISCORD_BOT_TOKEN" ]]; then
  platform_ok=true
elif [[ -n "$SLACK_BOT_TOKEN" && -n "$SLACK_APP_TOKEN" ]]; then
  platform_ok=true
fi

if ! $platform_ok; then
  error "No messaging platform configured. Set one of:"
  echo "  • TELEGRAM_BOT_TOKEN"
  echo "  • DISCORD_BOT_TOKEN"
  echo "  • SLACK_BOT_TOKEN + SLACK_APP_TOKEN"
  echo ""
  echo "  Add via: railway variables --set KEY=value"
  MISSING=true
fi

# ── Partial Slack config warning ─────────────────────────────────────────────
if [[ -n "$SLACK_BOT_TOKEN" && -z "$SLACK_APP_TOKEN" ]] || \
   [[ -z "$SLACK_BOT_TOKEN" && -n "$SLACK_APP_TOKEN" ]]; then
  warn "Slack requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN."
  MISSING=true
fi

# ── Check: volume mount ──────────────────────────────────────────────────────
required_mount=$(python3 -c "
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('railway.toml','rb') as f:
    d = tomllib.load(f)
print(d.get('deploy',{}).get('requiredMountPath',''))
" 2>/dev/null || echo "")

if [[ -n "$required_mount" ]]; then
  echo "[deploy] Note: railway.toml requires a volume mounted at ${required_mount}."
  echo "         Make sure you've added a volume in the Railway dashboard before deploying."
fi

# ── Abort if anything is missing ─────────────────────────────────────────────
if [[ "${MISSING:-false}" == "true" ]]; then
  echo ""
  error "Fix the above issues, then re-run: ./deploy.sh"
  exit 1
fi

if [[ "$RESET_STATE" == "true" ]]; then
  warn "--reset-state will delete persisted Hermes state before deploy:"
  echo "  • /data/.hermes"
  echo "  • /data/workspace"
  echo "  • /data/.claude"
  echo ""
  warn "This resets agent memory, sessions, pairing state, ByteRover state, workspace files, and the persisted Radius wallet."
  echo "[deploy] Resetting persisted state over Railway SSH..."
  railway ssh rm -rf /data/.hermes /data/workspace /data/.claude
  railway ssh mkdir -p /data /data/workspace
  success "Persistent state cleared."
fi

success "Environment checks passed. Deploying..."
if [[ ${#RAILWAY_UP_ARGS[@]} -gt 0 ]]; then
  exec railway up "${RAILWAY_UP_ARGS[@]}"
else
  exec railway up
fi
