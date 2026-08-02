from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


TEMPLATE_ROOT = Path(__file__).resolve().parents[1]
SKILLS_REPO = Path(os.environ.get("RADIUS_SKILLS_REPO_CHECKOUT", "/app/skills"))
EXPECTED_SKILLS = {"radius-dev", "x402", "dripping-faucet", "radius-agent-ops"}
EXPECTED_TOOLS = {
    "radius_wallet_address",
    "radius_balance",
    "radius_send_sbc",
    "radius_send_rusd",
    "radius_tx_status",
    "radius_chain_info",
}


class RadiusSkillsBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        if not SKILLS_REPO.exists():
            self.skipTest(f"Radius skills checkout not available at {SKILLS_REPO}")

    def test_skills_repo_has_hermes_install_smoke_tests(self):
        self.assertTrue((SKILLS_REPO / "scripts" / "hermes_install_smoke.py").exists())
        self.assertTrue((SKILLS_REPO / "tests" / "hermes" / "test_radius_skills_install.py").exists())
        result = subprocess.run(
            [sys.executable, str(SKILLS_REPO / "scripts" / "hermes_install_smoke.py"), "--repo-root", str(SKILLS_REPO)],
            text=True,
            capture_output=True,
            cwd=SKILLS_REPO,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(EXPECTED_TOOLS, set(payload["tools"]))
        self.assertEqual(EXPECTED_SKILLS, set(payload["skills"]))

    def test_template_bootstrap_model_exposes_external_skills_and_radius_cast_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hermes_home = tmp_path / ".hermes"
            radius_skills_dir = tmp_path / "external-skills" / "radius-skills"
            plugins_dir = hermes_home / "plugins"
            config_file = hermes_home / "config.yaml"
            shutil.copytree(SKILLS_REPO, radius_skills_dir, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            shutil.copytree(radius_skills_dir / "adapters" / "hermes" / "radius-cast", plugins_dir / "radius-cast")
            hermes_home.mkdir(parents=True, exist_ok=True)

            skill_roots = sorted({str(path.parent.parent) for path in radius_skills_dir.rglob("SKILL.md")})
            manifest = {
                "source": str(radius_skills_dir),
                "roots": skill_roots,
                "skills": [
                    {"name": path.parent.name, "path": str(path.parent), "root": str(path.parent.parent)}
                    for path in sorted(radius_skills_dir.rglob("SKILL.md"))
                ],
            }
            (hermes_home / "vendored-skills.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if yaml is None:
                config_file.write_text("skills:\n  external_dirs: []\nplugins:\n  enabled: []\n", encoding="utf-8")
            else:
                config_file.write_text(yaml.dump({"skills": {"external_dirs": []}, "plugins": {"enabled": []}}), encoding="utf-8")

            for skill_name in EXPECTED_SKILLS:
                self.assertIn(skill_name, {item["name"] for item in manifest["skills"]})
            self.assertIn(str(radius_skills_dir / "skills"), manifest["roots"])
            self.assertTrue((radius_skills_dir / "runtime" / "python" / "radius_wallet_runtime.py").exists())

            probe = r'''
import importlib.util, json, os
from pathlib import Path
class Ctx:
    def __init__(self):
        self.tools=[]
        self.hooks=[]
    def register_tool(self, **kwargs):
        self.tools.append(kwargs)
    def register_hook(self, name, callback):
        self.hooks.append((name, callback))
plugin = Path(os.environ["HERMES_HOME"]) / "plugins" / "radius-cast" / "__init__.py"
spec = importlib.util.spec_from_file_location("radius_cast_template_bootstrap", plugin)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
ctx = Ctx()
mod.register(ctx)
print(json.dumps({"tools": sorted(tool["name"] for tool in ctx.tools), "hooks": sorted(name for name, _ in ctx.hooks)}))
'''
            env = os.environ.copy()
            env.update({"HERMES_HOME": str(hermes_home), "RADIUS_SKILLS_DIR": str(radius_skills_dir)})
            result = subprocess.run([sys.executable, "-c", probe], text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(EXPECTED_TOOLS, set(payload["tools"]))
            self.assertIn("pre_llm_call", set(payload["hooks"]))

    def test_template_dockerfile_bootstraps_latest_radius_skills_snapshot(self):
        dockerfile = (TEMPLATE_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("git clone --depth 1 https://github.com/radiustechsystems/skills.git /app/vendor/radius-skills", dockerfile)
        entrypoint = (TEMPLATE_ROOT / "scripts" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("Bootstrapping Radius external skills from image snapshot", entrypoint)
        self.assertIn("Registering vendored Radius skill directories as Hermes external skill dirs", entrypoint)
        self.assertIn("Vendored Radius marketplace skills are exposed to Hermes via", entrypoint)


if __name__ == "__main__":
    unittest.main()
