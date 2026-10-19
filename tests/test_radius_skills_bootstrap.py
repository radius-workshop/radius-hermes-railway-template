"""Exercise the actual boot discovery/config code with isolated skill fixtures."""
from __future__ import annotations

import importlib.util
import json
import os
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


IS_TRUE = "is_true() {" + ENTRYPOINT.split("is_true() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
SKILLS_BOOTSTRAP = ENTRYPOINT.split("# --- radius skills bootstrap begin ---", 1)[1].split("# --- radius skills bootstrap end ---", 1)[0]
TOOLS_ORIGIN = "https://github.com/radiustechsystems/tools.git"


def git_checkout(path, origin, skill):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    skill_dir = path / "plugins" / "radius" / "skills" / skill
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: " + skill + "\n---\n")


def origin_of(path):
    return subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], check=True, capture_output=True, text=True).stdout.strip()


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


class RadiusSkillsMigrationTests(unittest.TestCase):
    def run_bootstrap(self, skills_dir, snapshot, bootstrap="true"):
        env = {**os.environ, "RADIUS_SKILLS_DIR": str(skills_dir), "RADIUS_SKILLS_BOOTSTRAP_FROM_IMAGE": bootstrap,
               "radius_skills_snapshot": str(snapshot)}
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", IS_TRUE + SKILLS_BOOTSTRAP], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshot = Path(self.tmp.name) / "snapshot"
        git_checkout(self.snapshot, TOOLS_ORIGIN, "sell-api")
        self.skills_dir = Path(self.tmp.name) / "radius-skills"

    def test_archived_skills_checkout_is_replaced_by_the_tools_snapshot(self):
        for origin in ("https://github.com/radiustechsystems/skills.git", "https://github.com/radiustechsystems/skills/"):
            with self.subTest(origin=origin):
                if self.skills_dir.exists():
                    subprocess.run(["rm", "-rf", str(self.skills_dir)], check=True)
                git_checkout(self.skills_dir, origin, "radius-dev")
                out = self.run_bootstrap(self.skills_dir, self.snapshot)
                self.assertIn("Replacing the archived radiustechsystems/skills checkout", out)
                self.assertEqual(origin_of(self.skills_dir), TOOLS_ORIGIN)
                self.assertTrue((self.skills_dir / "plugins/radius/skills/sell-api/SKILL.md").exists())
                self.assertFalse((self.skills_dir / "plugins/radius/skills/radius-dev").exists())

    def test_checkouts_of_other_repositories_are_kept(self):
        fork = "https://github.com/acme/radius-skills-fork.git"
        git_checkout(self.skills_dir, fork, "radius-dev")
        self.run_bootstrap(self.skills_dir, self.snapshot)
        self.assertEqual(origin_of(self.skills_dir), fork)
        self.assertTrue((self.skills_dir / "plugins/radius/skills/radius-dev/SKILL.md").exists())

    def test_archived_checkout_is_kept_when_image_bootstrap_is_disabled(self):
        archived = "https://github.com/radiustechsystems/skills.git"
        git_checkout(self.skills_dir, archived, "radius-dev")
        self.run_bootstrap(self.skills_dir, self.snapshot, bootstrap="false")
        self.assertEqual(origin_of(self.skills_dir), archived)

    def test_missing_checkout_is_bootstrapped_from_the_snapshot(self):
        self.run_bootstrap(self.skills_dir, self.snapshot)
        self.assertEqual(origin_of(self.skills_dir), TOOLS_ORIGIN)
        self.assertTrue((self.skills_dir / "plugins/radius/skills/sell-api/SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
