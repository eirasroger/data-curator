"""Loads the local API key, kept outside the repo. Cloud Run uses Secret Manager."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PATH = Path.home() / ".config" / "data-curator" / ".env"
LEGACY_PATH = ROOT / ".env"


def env_path() -> Path | None:
    override = os.environ.get("DATA_CURATOR_ENV")
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    if DEFAULT_PATH.is_file():
        return DEFAULT_PATH
    return LEGACY_PATH if LEGACY_PATH.is_file() else None


def load() -> Path | None:
    """Load the key into the environment. Returns the file read, or None."""
    path = env_path()
    if path is None:
        return None

    if path == LEGACY_PATH:
        print(f"warning: {path} is inside the repo; move it to {DEFAULT_PATH}",
              file=sys.stderr)

    try:
        from dotenv import load_dotenv
    except ImportError:
        return None

    load_dotenv(path)
    return path


__all__ = ["DEFAULT_PATH", "env_path", "load"]
