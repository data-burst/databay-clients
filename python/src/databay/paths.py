"""Where the client keeps its config and its state."""

from __future__ import annotations

import os
from pathlib import Path

from databay.settings import ENV_CONFIG_DIR, ENV_STATE_DIR


def config_dir() -> Path:
    """``$DATABAY_CONFIG_DIR`` when set, else the platform's config dir (``$XDG_CONFIG_HOME/databay`` on Linux)."""
    explicit = os.environ.get(ENV_CONFIG_DIR)
    if explicit:
        return Path(explicit)
    import platformdirs  # noqa: PLC0415

    return Path(platformdirs.user_config_dir("databay", appauthor=False))


def state_dir() -> Path:
    """``$DATABAY_STATE_DIR`` when set, else the platform's state dir (``~/.local/state/databay`` on Linux)."""
    explicit = os.environ.get(ENV_STATE_DIR)
    if explicit:
        return Path(explicit)
    import platformdirs  # noqa: PLC0415

    return Path(platformdirs.user_state_dir("databay", appauthor=False))


def config_file() -> Path:
    return config_dir() / "config.toml"
