import subprocess
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from scripts.agent_server.main import app
from scripts.agent_server.version import VERSION


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_runtime_liveness_and_discovery_report_canonical_version(self):
        with TestClient(app) as client:
            response = client.get("/livez")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok", "version": VERSION})
            card = client.get("/.well-known/agent-card.json")
            self.assertEqual(card.status_code, 200)
            self.assertEqual(card.json()["version"], VERSION)
            self.assertEqual(client.get("/health").status_code, 401)

    def test_release_validation_rejects_mismatched_tag(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate_release.py"), "--tag", "v999.0.0"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"release tag must be v{VERSION}", result.stderr)


if __name__ == "__main__":
    unittest.main()
