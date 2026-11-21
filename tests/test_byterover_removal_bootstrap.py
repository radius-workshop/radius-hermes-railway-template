"""Exercise the entrypoint's one-time ByteRover -> built-in memory migration."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (ROOT / "scripts/entrypoint.sh").read_text()
SECTION = ENTRYPOINT.split("memory: ByteRover is no longer installed", 1)[1]
MIGRATION = SECTION.split("<<'PYEOF'", 1)[1].split("\n", 1)[1].split("\nPYEOF", 1)[0]


class ByteRoverRemovalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.config = self.home / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def migrate(self):
        env = dict(os.environ, HERMES_HOME=str(self.home))
        return subprocess.run([sys.executable, "-c", MIGRATION], env=env, text=True, capture_output=True)

    def test_byterover_provider_is_reset_and_siblings_kept(self):
        self.config.write_text("model: keep-me\nmemory:\n  provider: ByteRover\n  nudge_interval: 10\n")
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = yaml.safe_load(self.config.read_text())
        self.assertEqual(cfg["memory"], {"provider": "", "nudge_interval": 10})
        self.assertEqual(cfg["model"], "keep-me")

    def test_other_provider_is_left_alone(self):
        original = "memory:\n  provider: mem0\n"
        self.config.write_text(original)
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_text(), original)

    def test_config_without_memory_section_is_untouched(self):
        original = "model: x\n"
        self.config.write_text(original)
        self.assertEqual(self.migrate().returncode, 0)
        self.assertEqual(self.config.read_text(), original)

    def test_failure_exits_nonzero_so_the_marker_is_kept(self):
        # No config.yaml: the shell only removes the marker when this exits 0.
        result = self.migrate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not reset memory.provider", result.stderr)

    def test_migration_is_gated_on_the_old_marker(self):
        self.assertIn('BYTEROVER_MARKER="${HERMES_HOME}/.byterover/initialized"', SECTION)
        self.assertIn('if [[ -f "$BYTEROVER_MARKER" ]]; then', SECTION)


if __name__ == "__main__":
    unittest.main()
