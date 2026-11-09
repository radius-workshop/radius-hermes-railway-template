"""Exercise the entrypoint's RADIUS_INFERENCE_PROVIDER -> config.yaml bridge and the LLM_MODEL sed."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (ROOT / "scripts/entrypoint.sh").read_text()
SECTION = ENTRYPOINT.split("Applying inference provider", 1)[1]
BRIDGE = SECTION.split("<<'PYEOF'", 1)[1].split("\n", 1)[1].split("\nPYEOF", 1)[0]
SED_BLOCK = ENTRYPOINT.split("# Ensure model is set in config.yaml", 1)[1].split("\n", 1)[1].split("\n# === Inference provider", 1)[0]
PROVIDER_CHECK = ENTRYPOINT.split("has_valid_provider_config() {", 1)[1].split("\n}\n", 1)[0]

PROXY_KEY = "k" * 64
VARS = ("RADIUS_INFERENCE_PROVIDER", "CF_X402_MODEL", "CF_X402_PORT", "CF_X402_PROXY_KEY", "CF_X402_CONTEXT_LENGTH",
        "LLM_MODEL", "OPENROUTER_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")


class CloudflareProviderBootstrapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.config = self.home / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **env_vars):
        env = {k: v for k, v in os.environ.items() if k not in VARS}
        env.update(HERMES_HOME=str(self.home), **env_vars)
        return env

    def boot(self, expect_ok=True, **env_vars):
        """LLM_MODEL sed, then the provider bridge, in entrypoint order."""
        env = self.env(**env_vars)
        sed = subprocess.run(["bash", "-c", f'set -euo pipefail\nCONFIG_FILE="$HERMES_HOME/config.yaml"\n{SED_BLOCK}'],
                             env=env, text=True, capture_output=True)
        self.assertEqual(sed.returncode, 0, sed.stderr)
        result = subprocess.run([sys.executable, "-c", BRIDGE], env=env, text=True, capture_output=True)
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def load(self):
        return yaml.safe_load(self.config.read_text()) or {}

    def select(self, **extra):
        return self.boot(RADIUS_INFERENCE_PROVIDER="cloudflare-x402", CF_X402_PROXY_KEY=PROXY_KEY, **extra)

    def test_unset_leaves_existing_config_untouched(self):
        original = "model: openai/gpt-5.4-nano\nterminal:\n  backend: local\n"
        self.config.write_text(original)
        self.boot()
        self.assertEqual(self.config.read_text(), original)
        self.boot(LLM_MODEL="anthropic/claude-x")
        self.assertEqual(self.load()["model"], "anthropic/claude-x")

    def test_selecting_points_hermes_at_the_local_proxy(self):
        self.config.write_text("model: openai/gpt-5.4-nano\nterminal:\n  backend: local\n")
        self.select(CF_X402_MODEL="google/gemma-4-26b-a4b-it", CF_X402_CONTEXT_LENGTH="65536")
        cfg = self.load()
        self.assertEqual(cfg["model"], {
            "provider": "custom", "default": "google/gemma-4-26b-a4b-it",
            "base_url": "http://127.0.0.1:8650/v1", "api_key": PROXY_KEY, "context_length": 65536,
        })
        self.assertEqual(cfg["terminal"], {"backend": "local"})

    def test_llm_model_does_not_break_the_mapping(self):
        self.config.write_text("model: openai/gpt-5.4-nano\n")
        self.select(LLM_MODEL="openai/gpt-5.4-nano")
        # Second boot: the sed must leave the `model:` mapping alone (it used to rewrite its first line).
        self.select(LLM_MODEL="openai/gpt-5.4-nano")
        self.assertEqual(self.load()["model"]["base_url"], "http://127.0.0.1:8650/v1")

    def test_switching_back_restores_the_previous_model(self):
        self.config.write_text("model: openai/gpt-5.4-nano\n")
        self.select()
        self.select()
        self.boot()
        self.assertEqual(self.load()["model"], "openai/gpt-5.4-nano")
        self.assertFalse((self.home / ".inference-provider-managed.json").exists())

    def test_switching_back_prefers_llm_model(self):
        self.config.write_text("model: openai/gpt-5.4-nano\n")
        self.select()
        self.boot(LLM_MODEL="anthropic/claude-x")
        self.assertEqual(self.load()["model"], "anthropic/claude-x")

    def test_switching_back_keeps_an_operator_edit(self):
        self.config.write_text("model: openai/gpt-5.4-nano\n")
        self.select()
        cfg = self.load()
        cfg["model"] = {"provider": "openrouter", "default": "x/y"}
        self.config.write_text(yaml.safe_dump(cfg))
        self.boot()
        self.assertEqual(self.load()["model"], {"provider": "openrouter", "default": "x/y"})

    def test_unreadable_config_fails_the_boot_when_selected(self):
        self.config.write_text("model: [unclosed\n")
        result = self.boot(expect_ok=False, RADIUS_INFERENCE_PROVIDER="cloudflare-x402", CF_X402_PROXY_KEY=PROXY_KEY)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_text(), "model: [unclosed\n")

    def provider_ok(self, **env_vars):
        script = f"has_valid_provider_config() {{{PROVIDER_CHECK}\n}}\nhas_valid_provider_config"
        return subprocess.run(["bash", "-c", script], env=self.env(**env_vars), text=True, capture_output=True).returncode == 0

    def test_provider_check(self):
        self.assertTrue(self.provider_ok(OPENROUTER_API_KEY="x"))
        self.assertFalse(self.provider_ok())
        self.assertTrue(self.provider_ok(RADIUS_INFERENCE_PROVIDER="cloudflare-x402", CLOUDFLARE_ACCOUNT_ID="a", CLOUDFLARE_API_TOKEN="t"))
        # Selected but incomplete fails even when OpenRouter is configured: no silent fallback.
        self.assertFalse(self.provider_ok(RADIUS_INFERENCE_PROVIDER="cloudflare-x402", OPENROUTER_API_KEY="x"))


@unittest.skipUnless(shutil.which("node") and (ROOT / "scripts/cf_x402_proxy/node_modules").is_dir(),
                     "cf_x402_proxy dependencies not installed (npm ci in scripts/cf_x402_proxy)")
class CloudflareProxyNodeTests(unittest.TestCase):
    def test_node_suite(self):
        result = subprocess.run(["npm", "test", "--silent"], cwd=ROOT / "scripts/cf_x402_proxy", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-4000:])


if __name__ == "__main__":
    unittest.main()
