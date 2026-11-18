#!/usr/bin/env bash
# Installed as `sbc`. Runs the SBC Agent Stack CLI (@stablecoin.xyz/cli) with
# this agent's Radius wallet as its signing key, so `sbc swap`, `sbc gas`,
# `sbc swap balance` and `sbc key show` work with no setup.
#
# radius-cli's keystore is a standard Ethereum v3 keystore, which sbc reads as
# is: nothing is copied and no key is put in the environment. Defaults are
# worked out here, not in the entrypoint, so they also apply in shells that do
# not inherit the container environment (the Hermes terminal, `railway ssh`).
# Anything the operator sets wins. SBC_USE_RADIUS_WALLET=false turns this off.
set -u

if [[ "${SBC_USE_RADIUS_WALLET:-true}" != "false" && "${SBC_USE_RADIUS_WALLET:-true}" != "0" ]]; then
  radius_home="${RADIUS_HOME:-${HERMES_HOME:-/data/.hermes}/.radius-cli}"
  keystore="${radius_home}/keystore.json"

  # sbc refuses two EVM key sources, so only fill in when none is set.
  if [[ -z "${SBC_EVM_KEYSTORE:-}${SBC_EVM_KEY_FILE:-}${SBC_EVM_PRIVATE_KEY:-}" && -f "$keystore" ]]; then
    export SBC_EVM_KEYSTORE="$keystore"
    # The template's keystore has an empty password (file mode 0600). sbc takes
    # passwords from a file only. A keystore the operator gave a password is
    # left locked: sbc then reports that it needs a password file.
    if [[ -z "${SBC_EVM_KEYSTORE_PASSWORD_FILE:-}" ]] \
      && jq -e '.passwordless == true' "${radius_home}/config.json" >/dev/null 2>&1; then
      password_file="${radius_home}/sbc-empty-password"
      [[ -f "$password_file" ]] || (umask 077 && : > "$password_file") 2>/dev/null || true
      [[ -f "$password_file" ]] && export SBC_EVM_KEYSTORE_PASSWORD_FILE="$password_file"
    fi
  fi

  # Follow the wallet's network. The entrypoint records it for shells that do
  # not inherit RADIUS_NETWORK. Unknown means testnet, the template default.
  if [[ -z "${SBC_SWAP_NET:-}" ]]; then
    network="${RADIUS_NETWORK:-$(cat "${radius_home}/network" 2>/dev/null || true)}"
    [[ "$network" == "mainnet" ]] || export SBC_SWAP_NET=testnet
  fi

  if [[ -z "${SBC_RADIUS_RPC_URL:-}" && -n "${RADIUS_RPC_URL:-}" ]]; then
    export SBC_RADIUS_RPC_URL="$RADIUS_RPC_URL"
  fi
fi

if [[ -n "${SBC_CLI_BIN:-}" ]]; then
  exec "$SBC_CLI_BIN" "$@"
fi
exec node "${SBC_CLI_JS:-/usr/local/lib/node_modules/@stablecoin.xyz/cli/dist/index.js}" "$@"
