"""The `sbc` wrapper points the SBC CLI at the agent's Radius wallet by default
and never overrides what the operator set."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "sbc-wrapper.sh"


class SbcWrapperTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / ".radius-cli"
        self.home.mkdir()
        # Stands in for the real CLI: prints the SBC_* environment it was given.
        self.stub = Path(tmp.name) / "sbc-stub"
        self.stub.write_text(
            "#!/usr/bin/env bash\n"
            "env | grep '^SBC_' | grep -v '^SBC_CLI_BIN=' | sort\n"
            "echo \"ARGS=$*\"\n"
        )
        self.stub.chmod(0o755)

    def wallet(self, passwordless=True):
        (self.home / "keystore.json").write_text('{"version": 3}')
        (self.home / "config.json").write_text(json.dumps({"passwordless": passwordless}))

    def run_wrapper(self, *args, **env):
        result = subprocess.run(
            ["bash", str(WRAPPER), *args],
            env={"PATH": os.environ["PATH"], "RADIUS_HOME": str(self.home),
                 "SBC_CLI_BIN": str(self.stub), **env},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_uses_the_radius_keystore_on_testnet_by_default(self):
        self.wallet()
        seen = self.run_wrapper("swap", "balance")
        self.assertEqual(seen["SBC_EVM_KEYSTORE"], str(self.home / "keystore.json"))
        password_file = Path(seen["SBC_EVM_KEYSTORE_PASSWORD_FILE"])
        self.assertEqual(password_file.read_text(), "")
        # sbc refuses a password file other users can read.
        self.assertEqual(password_file.stat().st_mode & 0o077, 0)
        self.assertEqual(seen["SBC_SWAP_NET"], "testnet")
        self.assertEqual(seen["ARGS"], "swap balance")

    def test_mainnet_wallet_is_not_forced_to_testnet(self):
        self.wallet()
        self.assertNotIn("SBC_SWAP_NET", self.run_wrapper(RADIUS_NETWORK="mainnet"))
        # Shells that do not inherit RADIUS_NETWORK read what the entrypoint recorded.
        (self.home / "network").write_text("mainnet\n")
        self.assertNotIn("SBC_SWAP_NET", self.run_wrapper())
        # Nothing here opts in to unattended mainnet signing.
        self.assertNotIn("SBC_SWAP_ALLOW_MAINNET", self.run_wrapper())

    def test_operator_settings_win(self):
        self.wallet()
        seen = self.run_wrapper(SBC_EVM_KEY_FILE="/operator/key", SBC_SWAP_NET="mainnet")
        self.assertNotIn("SBC_EVM_KEYSTORE", seen)
        self.assertNotIn("SBC_EVM_KEYSTORE_PASSWORD_FILE", seen)
        self.assertEqual(seen["SBC_SWAP_NET"], "mainnet")

    def test_password_protected_keystore_stays_locked(self):
        self.wallet(passwordless=False)
        seen = self.run_wrapper()
        self.assertIn("SBC_EVM_KEYSTORE", seen)
        self.assertNotIn("SBC_EVM_KEYSTORE_PASSWORD_FILE", seen)

    def test_no_wallet_yet_and_opt_out(self):
        self.assertNotIn("SBC_EVM_KEYSTORE", self.run_wrapper())
        self.wallet()
        seen = self.run_wrapper(SBC_USE_RADIUS_WALLET="false")
        self.assertEqual(seen, {"SBC_USE_RADIUS_WALLET": "false", "ARGS": ""})

    def test_passes_the_radius_rpc_override(self):
        self.wallet()
        seen = self.run_wrapper(RADIUS_RPC_URL="https://rpc.example")
        self.assertEqual(seen["SBC_RADIUS_RPC_URL"], "https://rpc.example")


if __name__ == "__main__":
    unittest.main()
