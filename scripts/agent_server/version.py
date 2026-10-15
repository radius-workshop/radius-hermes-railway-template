"""The template release version, independent of protocol and plugin versions."""
from pathlib import Path
import re


VERSION = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
match = re.fullmatch(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
    VERSION,
)
if not match or any(
    identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
    for identifier in (match.group("prerelease") or "").split(".")
):
    raise ValueError("VERSION must contain a semantic version")
