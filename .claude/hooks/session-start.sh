#!/usr/bin/env bash
# Prepare a Claude Code cloud session: a Python 3.11 venv with the same
# dependencies CI installs. The system python3 here is 3.13 and Debian-managed.
set -euo pipefail

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

venv="$PWD/.venv"
stamp="$venv/.requirements.sha256"
want=$(cat requirements-dev.txt scripts/agent_server/requirements.txt | sha256sum | cut -d' ' -f1)

if [ ! -x "$venv/bin/python" ] || [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
  python3.11 -m venv "$venv"
  "$venv/bin/python" -m pip install -q --disable-pip-version-check -r requirements-dev.txt
  "$venv/bin/python" -m pip check
  echo "$want" > "$stamp"
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ] && ! grep -qs "VIRTUAL_ENV=\"$venv\"" "$CLAUDE_ENV_FILE"; then
  {
    echo "export VIRTUAL_ENV=\"$venv\""
    echo "export PATH=\"$venv/bin:\$PATH\""
  } >> "$CLAUDE_ENV_FILE"
fi
