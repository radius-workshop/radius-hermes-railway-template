#!/usr/bin/env python3
"""Enter one rotated provider credential directly into a Railway egress service."""
from __future__ import annotations

import argparse
import getpass
import re
import subprocess
import sys
import warnings

from iron_egress import CATALOG


def target(value: str) -> str:
    if not value.strip() or value.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise argparse.ArgumentTypeError("Use a non-empty target name or ID without control characters.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=target)
    parser.add_argument("--environment", required=True, type=target)
    parser.add_argument("--service", required=True, type=target)
    parser.add_argument("--name", required=True, choices=sorted(CATALOG))
    args = parser.parse_args(argv)

    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Run this helper in your local interactive terminal; piped credential input is refused.", file=sys.stderr)
        return 1

    print(f"Project: {args.project}\nEnvironment: {args.environment}\nService: {args.service}\nCredential: {args.name}")
    print("This writes a real credential. The target must be your egress service, never the agent service.")
    print("No deployment will be triggered. The credential will not be displayed.")
    try:
        confirmation = input("Confirm the egress target by typing its service name or ID exactly: ")
        if confirmation != args.service:
            print("Cancelled; no variable was changed.")
            return 1
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            credential = getpass.getpass("New credential (hidden): ")
        if not credential.strip() or "\n" in credential or "\r" in credential or "\x00" in credential:
            print("Credential must be a non-empty single line; no variable was changed.", file=sys.stderr)
            return 1
        if args.name == "TELEGRAM_BOT_TOKEN" and not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", credential):
            print("Paste only the bot token from BotFather, without quotes or a URL.", file=sys.stderr)
            return 1
        command = [
            "railway", "variable", "set", args.name, "--stdin", "--skip-deploys",
            "--project", args.project, "--environment", args.environment, "--service", args.service,
        ]
        result = subprocess.run(
            command, input=credential, text=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Cancelled; credential entry was interrupted or could not be hidden.", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print("Railway did not confirm the update. Check CLI access and variable status before retrying.", file=sys.stderr)
        return 1
    if result.returncode:
        print("Railway did not confirm the update. Check CLI access and variable status before retrying.", file=sys.stderr)
        return 1
    print(f"Updated {args.name} on {args.service}; deployment was not triggered. Provider validity is not yet checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
