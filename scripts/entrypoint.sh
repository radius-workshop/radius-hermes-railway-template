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
  SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_ALLOW_ALL_USERS SLACK_HOME_CHANNEL SLACK_HOME_CHANNEL_NAME WHATSAPP_ENABLED WHATSAPP_ALLOWED_USERS \
  GATEWAY_ALLOW_ALL_USERS \
  FIRECRAWL_API_KEY NOUS_API_KEY BROWSERBASE_API_KEY BROWSERBASE_PROJECT_ID BROWSERBASE_PROXIES BROWSERBASE_ADVANCED_STEALTH BROWSER_SESSION_TIMEOUT BROWSER_INACTIVITY_TIMEOUT FAL_KEY ELEVENLABS_API_KEY VOICE_TOOLS_OPENAI_KEY \
  TINKER_API_KEY WANDB_API_KEY RL_API_URL GITHUB_TOKEN BYTEROVER_API_KEY BYTEROVER_LOCAL LINEAR_API_KEY LINEAR_TEAM_ID LINEAR_PROJECT_ID \
  TERMINAL_BACKEND TERMINAL_DOCKER_IMAGE TERMINAL_SINGULARITY_IMAGE TERMINAL_MODAL_IMAGE TERMINAL_CWD TERMINAL_TIMEOUT TERMINAL_LIFETIME_SECONDS TERMINAL_CONTAINER_CPU TERMINAL_CONTAINER_MEMORY TERMINAL_CONTAINER_DISK TERMINAL_CONTAINER_PERSISTENT TERMINAL_SANDBOX_DIR TERMINAL_SSH_HOST TERMINAL_SSH_USER TERMINAL_SSH_PORT TERMINAL_SSH_KEY SUDO_PASSWORD \
  WEB_TOOLS_DEBUG VISION_TOOLS_DEBUG MOA_TOOLS_DEBUG IMAGE_TOOLS_DEBUG CONTEXT_COMPRESSION_ENABLED CONTEXT_COMPRESSION_THRESHOLD CONTEXT_COMPRESSION_MODEL HERMES_MAX_ITERATIONS HERMES_TOOL_PROGRESS HERMES_TOOL_PROGRESS_MODE \
  RADIUS_PRIVATE_KEY RADIUS_WALLET_ADDRESS RADIUS_NETWORK RADIUS_AUTO_FUND \
  ERC8004_NETWORK ERC8004_TESTNET_RPC_URL ERC8004_TESTNET_REGISTRY ERC8004_TESTNET_EXPLORER_URL ERC8004_TESTNET_CHAIN_ID ERC8004_MAINNET_RPC_URL ERC8004_MAINNET_REGISTRY ERC8004_MAINNET_EXPLORER_URL ERC8004_MAINNET_CHAIN_ID ERC8004_GAS_LIMIT ERC8004_MAX_AGENT_URI_BYTES \
  AGENT_NAME AGENT_DESCRIPTION AGENT_IMAGE AGENT_ACTIVE AGENT_X402_SUPPORT AGENT_SUPPORTED_TRUST AGENT_A2A_VERSION AGENT_ERC8004_ID AGENT_ERC8004_REGISTRY AGENT_ANS_NAME AGENT_ANS_AGENT_ID AGENT_ANS_HOST AGENT_ANS_STATUS AGENT_WALLET AGENT_EMAIL AGENT_ENS \
  PARA_API_KEY PARA_SECRET_API_KEY PARA_ENVIRONMENT PARA_REST_BASE_URL PARA_WALLET_ID \
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
if [[ ! -f "$RADIUS_WALLET_MARKER" ]]; then
  echo "[bootstrap] Setting up Radius wallet..."
  if python3 /app/scripts/radius/wallet_init.py; then
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$RADIUS_WALLET_MARKER"
    # Reload keys generated during init into the current process environment so
    # the agent server and JWT auth use the persistent wallet immediately.
    if [[ -f "$RADIUS_KEY_FILE" ]]; then
      export RADIUS_PRIVATE_KEY="$(cat "$RADIUS_KEY_FILE")"
    fi
    if [[ -f "$RADIUS_ADDR_FILE" ]]; then
      export RADIUS_WALLET_ADDRESS="$(cat "$RADIUS_ADDR_FILE")"
    fi

    # Persist generated values to .env for downstream tools.
    if [[ -n "${RADIUS_PRIVATE_KEY:-}" ]] && ! grep -q "^RADIUS_PRIVATE_KEY=" "$ENV_FILE" 2>/dev/null; then
      echo "RADIUS_PRIVATE_KEY=${RADIUS_PRIVATE_KEY}" >> "$ENV_FILE"
    fi
    if [[ -n "${RADIUS_WALLET_ADDRESS:-}" ]] && ! grep -q "^RADIUS_WALLET_ADDRESS=" "$ENV_FILE" 2>/dev/null; then
      echo "RADIUS_WALLET_ADDRESS=${RADIUS_WALLET_ADDRESS}" >> "$ENV_FILE"
    fi
    echo "[bootstrap] Radius wallet ready: $(cat "$RADIUS_ADDR_FILE" 2>/dev/null || echo 'unknown')"
  else
    echo "[bootstrap] WARNING: Radius wallet setup failed. Will retry on next boot." >&2
  fi
else
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

# === vendored skills: persist Radius external directory and discover skill roots ===
RADIUS_SKILLS_DIR="${RADIUS_SKILLS_DIR:-/data/.hermes/external-skills/radius-skills}"
RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE="${RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE:-true}"
mkdir -p "$(dirname "${RADIUS_SKILLS_DIR}")"

if [[ ! -f "${RADIUS_SKILLS_DIR}/.git/HEAD" ]]; then
  if is_true "${RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE}" && [[ -d "/app/vendor/radius-skills" ]]; then
    echo "[bootstrap] Bootstrapping Radius external skills from image snapshot into ${RADIUS_SKILLS_DIR}..."
    rm -rf "${RADIUS_SKILLS_DIR}"
    cp -a /app/vendor/radius-skills "${RADIUS_SKILLS_DIR}"
  else
    echo "[bootstrap] Radius external skills directory missing and bootstrap disabled; creating empty directory at ${RADIUS_SKILLS_DIR}."
    mkdir -p "${RADIUS_SKILLS_DIR}"
  fi
fi

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
link_into_workspace /app/AGENTS.md "${MESSAGING_CWD}/AGENTS.md" true
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
