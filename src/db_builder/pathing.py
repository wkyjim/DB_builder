"""Import path helper for scripts run from the scripts directory."""

from __future__ import annotations

import sys
from pathlib import Path


def add_project_src_to_path() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    src_path = project_root / "src"

    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    return project_root

