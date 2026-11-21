"""The image runs as an unprivileged user, and the entrypoint says so clearly
when the data volume is not writable for it."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NonRootImageTests(unittest.TestCase):
    def test_final_stage_runs_as_uid_10001(self):
        final_stage = (ROOT / "Dockerfile").read_text().rsplit("\nFROM ", 1)[1]
        users = [line.split(None, 1)[1].strip() for line in final_stage.splitlines()
                 if line.startswith("USER ")]
        # Numeric, so Kubernetes can verify runAsNonRoot without a passwd lookup.
        self.assertEqual(users, ["10001:10001"])

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_entrypoint_rejects_unwritable_data_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            data.chmod(0o555)
            try:
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts" / "entrypoint.sh")],
                    env={"PATH": os.environ["PATH"], "HOME": str(data),
                         "HERMES_HOME": str(data / ".hermes")},
                    capture_output=True, text=True, timeout=30,
                )
            finally:
                data.chmod(0o755)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not writable by uid", result.stderr)
        self.assertIn("10001", result.stderr)


if __name__ == "__main__":
    unittest.main()
