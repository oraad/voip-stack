"""Locate coordinated sibling repositories in either workspace layout.

Historically the suite ran from a nested checkout whose siblings were cloned
next to the repository (``ROOT.parent``). The workspace now hosts the checkout
at its root with the siblings inside it, so resolution prefers the in-workspace
location, falls back to the sibling location, and stays overridable per repo
through the documented environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def sibling_repo(variable: str, name: str) -> Path:
    override = os.environ.get(variable)
    if override:
        return Path(override)
    for base in (WORKSPACE_ROOT, WORKSPACE_ROOT.parent):
        candidate = base / name
        if candidate.is_dir():
            return candidate
    return WORKSPACE_ROOT / name
