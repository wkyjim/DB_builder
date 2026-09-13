"""Load local secrets from the external DB_builder environment directory."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_external_env_dir() -> Path:
    container = PROJECT_ROOT.parent
    if container.name.casefold() == "hermes_pm":
        container = container.parent
    return container / "DB_builder_env"


DEFAULT_EXTERNAL_ENV_DIR = _default_external_env_dir()
TRUTHY_VALUES = {"1", "true", "yes", "on"}


def audit_mode_enabled() -> bool:
    """Return whether local secret files must remain inaccessible to this process."""
    return os.getenv("DB_BUILDER_AUDIT_MODE", "").strip().lower() in TRUTHY_VALUES


def external_env_dir() -> Path:
    """Return the private env root without requiring it to exist."""
    configured = os.getenv("DB_BUILDER_ENV_DIR")
    return Path(configured).expanduser() if configured else DEFAULT_EXTERNAL_ENV_DIR


def external_env_path(relative_path: str | Path = ".env") -> Path:
    return external_env_dir() / Path(relative_path)


def load_external_env(relative_path: str | Path = ".env", *, override: bool = False) -> Path:
    """Load one external env file, preserving already-set process variables."""
    path = external_env_path(relative_path)
    if audit_mode_enabled():
        return path
    if not path.is_file():
        return path

    if load_dotenv is not None:
        load_dotenv(path, override=override)
        return path

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)
    return path
