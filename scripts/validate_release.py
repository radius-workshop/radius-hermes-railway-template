#!/usr/bin/env python3
"""Validate the canonical version and, when supplied, its release tag."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent_server"))
from version import VERSION


parser = argparse.ArgumentParser()
parser.add_argument("--tag")
args = parser.parse_args()
if args.tag is not None and args.tag != f"v{VERSION}":
    parser.error(f"release tag must be v{VERSION}; got {args.tag!r}")
print(VERSION)
