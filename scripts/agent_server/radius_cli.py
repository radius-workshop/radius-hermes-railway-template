"""Thin wrapper around the `radius-cli` binary for the agent server.

radius-cli (npm `radius-cli`, from radiustechsystems/tools) owns the wallet
keystore, chain config, balances and signing. This module only locates the
binary and runs it with the template's RADIUS_HOME / RADIUS_NETWORK defaults;
RADIUS_RPC_URL and RADIUS_SBC_ADDRESS are read by radius-cli from the
environment directly.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

DEFAULT_NETWORK = "testnet"


def radius_home() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    return Path(os.environ.get("RADIUS_HOME", str(Path(hermes_home) / ".radius-cli")))


def radius_cli_cmd() -> list[str]:
    configured = os.environ.get("RADIUS_CLI_BIN", "").strip()
    if configured:
        return shlex.split(configured)
    resolved = shutil.which("radius-cli")
    if resolved:
        return [resolved]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "radius-cli"]
    raise RuntimeError("radius-cli is not installed or not on PATH")


def run_radius_cli(args: list[str], *, input_text: str | None = None, timeout: float = 20) -> str:
    env = {**os.environ, "RADIUS_HOME": str(radius_home())}
    env["RADIUS_NETWORK"] = env.get("RADIUS_NETWORK", "").strip() or DEFAULT_NETWORK
    # radius-cli treats an empty override as set; drop blanks so it falls back to network defaults.
    for key in ("RADIUS_RPC_URL", "RADIUS_SBC_ADDRESS"):
        if not env.get(key, "").strip():
            env.pop(key, None)
    proc = subprocess.run(
        [*radius_cli_cmd(), *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(msg or f"radius-cli exited with code {proc.returncode}")
    return (proc.stdout or "").strip()


def wallet_balance(address: str) -> dict:
    """`radius-cli --json wallet balance <address>`: {address, sbc, rusd, ...} in display units."""
    return json.loads(run_radius_cli(["--json", "wallet", "balance", address]))


def export_private_key() -> str:
    """Decrypt the radius-cli keystore and return its 0x private key.

    `wallet export` asks for confirmation; answer it on stdin, as the entrypoint does.
    """
    out = run_radius_cli(["wallet", "export"], input_text="y\n")
    match = re.search(r"0x[a-fA-F0-9]{64}", out)
    if not match:
        raise RuntimeError("could not parse private key from radius-cli wallet export")
    return match.group(0)
