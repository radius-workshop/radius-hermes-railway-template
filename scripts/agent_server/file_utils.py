"""Utilities for detecting and surfacing local file paths in model output."""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Iterable

PATH_RE = re.compile(r"(?P<path>/[\w\-./ ]+)")


def detect_file_paths(text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in PATH_RE.finditer(text or ""):
        candidate = match.group("path").strip().rstrip(".,:;)")
        if len(candidate) < 2:
            continue
        if candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    return paths


def infer_mime_type(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def replace_file_paths(text: str, replacements: dict[str, str]) -> str:
    rendered = text
    for original, replacement in replacements.items():
        rendered = rendered.replace(original, replacement)
    return rendered


def allowed_file_paths(paths: Iterable[str], allowed_roots: list[Path]) -> list[Path]:
    accepted: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if not resolved.is_file():
            continue
        if any(_is_within(resolved, root) for root in allowed_roots):
            accepted.append(resolved)
    return accepted


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
