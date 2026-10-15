#!/usr/bin/env python3
"""Apply the reviewed dependency compatibility overlay to the pinned Hermes source.

This changes dependency metadata only. Preserve the upstream Git revision and
record both source hashes plus this script's hash for image provenance.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import tomllib


OLD = "PyJWT[crypto]==2.13.0"
NEW = "PyJWT[crypto]==2.15.1"
MANIFEST = ".radius-dependency-overrides.json"


def apply(hermes_root: Path) -> dict:
    source = hermes_root / "pyproject.toml"
    before = source.read_bytes()
    text = before.decode("utf-8")
    dependencies = tomllib.loads(text).get("project", {}).get("dependencies", [])
    if dependencies.count(OLD) != 1 or text.count('"' + OLD + '"') != 1:
        raise ValueError("Hermes PyJWT declaration changed; review the compatibility overlay before building")
    after = text.replace('"' + OLD + '"', '"' + NEW + '"', 1).encode("utf-8")
    patch_name = ".radius-dependency-overrides.patch"
    patch = "".join(difflib.unified_diff(
        text.splitlines(keepends=True), after.decode("utf-8").splitlines(keepends=True),
        fromfile="a/pyproject.toml", tofile="b/pyproject.toml",
    )).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "source_file": "pyproject.toml",
        "before_sha256": hashlib.sha256(before).hexdigest(),
        "after_sha256": hashlib.sha256(after).hexdigest(),
        "patch_file": patch_name,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "patch_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "changes": [{"from": OLD, "to": NEW}],
    }
    source.write_bytes(after)
    (hermes_root / patch_name).write_bytes(patch)
    (hermes_root / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hermes_root", type=Path)
    args = parser.parse_args()
    apply(args.hermes_root)
