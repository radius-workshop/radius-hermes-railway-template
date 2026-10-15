#!/usr/bin/env python3
"""Run every repository test module, including namespace package directories."""
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="radius-hermes-tests-") as home:
        os.environ["HERMES_HOME"] = home
        os.environ["A2A_SESSION_ROOT"] = str(Path(home) / "a2a-sessions")
        os.environ["AGENT_SERVER_MOCK_DATA"] = "1"
        os.environ["PUBLIC_URL"] = "https://agent.example.com"
        import pytest

        return pytest.main([
            str(ROOT / "tests"), str(ROOT / "scripts"), str(ROOT / "plugins"),
            "-q", *sys.argv[1:],
        ])


if __name__ == "__main__":
    raise SystemExit(main())
