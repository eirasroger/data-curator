"""Where the local API key file lives. Local runs only.

Nothing deployed calls this - Cloud Run mounts the key from Secret Manager.
It is kept outside the working tree because .gitignore does nothing about
folder sync or an archive of the project directory.
"""

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
    """Load the key into the environment. Returns what it read, if anything.

    Never raises: no file and no python-dotenv both mean "no key", and the
    caller falls back to the stub provider.
    """
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
