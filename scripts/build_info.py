#!/usr/bin/env python3
"""Write public build provenance without collecting environment variables."""
import argparse
import hashlib
from importlib.metadata import distributions
import json
from pathlib import Path
import subprocess


parser = argparse.ArgumentParser()
parser.add_argument("--revision", required=True)
parser.add_argument("--radius-cli-version", required=True)
args = parser.parse_args()


def commit(path: str) -> str:
    return subprocess.check_output(
        ["git", "-C", path, "rev-parse", "HEAD"], text=True
    ).strip()


def hermes_dependency_overrides() -> dict:
    root = Path("/opt/hermes-agent")
    manifest = json.loads((root / ".radius-dependency-overrides.json").read_text())
    if hashlib.sha256((root / manifest["source_file"]).read_bytes()).hexdigest() != manifest["after_sha256"]:
        raise SystemExit("Hermes dependency source changed after applying the recorded compatibility overlay")
    if hashlib.sha256((root / manifest["patch_file"]).read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise SystemExit("Hermes dependency patch differs from its recorded hash")
    return manifest


actual_cli_version = subprocess.check_output(["radius-cli", "--version"], text=True).strip()
if actual_cli_version != args.radius_cli_version:
    raise SystemExit("Installed Radius CLI version differs from the requested build version")

print(json.dumps({
    "version": (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip(),
    "revision": args.revision,
    "hermes_commit": commit("/opt/hermes-agent"),
    "hermes_dependency_overrides": hermes_dependency_overrides(),
    "linear_skill_commit": commit("/app/scripts/linear-skill"),
    "radius_skills_commit": commit("/app/vendor/radius-skills"),
    "radius_cli_version": actual_cli_version,
    "node_version": subprocess.check_output(["node", "--version"], text=True).strip(),
    "python_packages": {
        package.metadata["Name"]: package.version for package in distributions()
    },
}, indent=2, sort_keys=True))
