"""Exercise the actual boot discovery/config code with isolated skill fixtures."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (ROOT / "scripts/entrypoint.sh").read_text()


def boot_python_after(marker):
    section = ENTRYPOINT.split(marker, 1)[1]
    return section.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]


class RadiusSkillsBootstrapTests(unittest.TestCase):
    def test_discovery_and_registration_are_idempotent_and_preserve_operator_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            source = Path(tmp) / "radius-skills"
            home.mkdir()
            for name in ("radius-dev", "dripping-faucet"):
                skill = source / "skills" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("---\nname: " + name + "\npublished: true\n---\nInstructions")
            config = home / "config.yaml"
            config.write_text(yaml.safe_dump({"skills": {"external_dirs": ["/operator/skills", "/app/vendor/radius-skills/skills"]}, "model": "keep-me"}))
            manifest_path = home / "vendored-skills.json"
            env = {**os.environ, "HERMES_HOME": str(home), "VENDORED_SKILLS_SOURCE": str(source), "VENDORED_SKILLS_MANIFEST": str(manifest_path)}
            for _ in range(2):
                for marker in ("Discovering vendored skills under", "Registering vendored Radius skill directories"):
                    result = subprocess.run([sys.executable, "-c", boot_python_after(marker)], env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual({s["name"] for s in manifest["skills"]}, {"radius-dev", "dripping-faucet"})
            self.assertEqual(manifest["roots"], [str(source / "skills")])
            config_data = yaml.safe_load(config.read_text())
            self.assertEqual(config_data["skills"]["external_dirs"], ["/operator/skills", str(source / "skills")])
            self.assertEqual(config_data["model"], "keep-me")

    def test_missing_optional_external_checkout_does_not_break_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            env = {**os.environ, "VENDORED_SKILLS_SOURCE": str(Path(tmp) / "absent"), "VENDORED_SKILLS_MANIFEST": str(manifest)}
            result = subprocess.run([sys.executable, "-c", boot_python_after("Discovering vendored skills under")], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(manifest.read_text())["skills"], [])

    def test_bundled_wallet_plugin_exposes_declared_cli_tools_without_wallet_access(self):
        spec = importlib.util.spec_from_file_location("radius_bootstrap_probe", ROOT / "plugins/radius-cast/__init__.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        class Context:
            def __init__(self):
                self.tools = []
            def register_tool(self, **kwargs):
                self.tools.append(kwargs["name"])
        context = Context()
        plugin.register(context)
        declared = yaml.safe_load((ROOT / "plugins/radius-cast/plugin.yaml").read_text())["provides_tools"]
        self.assertEqual(set(context.tools), set(declared))
        self.assertEqual(set(declared), {"radius_wallet_address", "radius_balance", "radius_send_sbc", "radius_tx_status"})


if __name__ == "__main__":
    unittest.main()
