"""
Exclusive file locks between databay processes: one per credential, one for ``config.toml``.

``fcntl.flock`` on ``<config dir>/locks/<name>.lock``. The kernel drops the lock when the process
exits, so a killed process never leaves a credential locked.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

from databay.paths import config_dir

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CONFIG_LOCK = "config"


def lock_path(name: str) -> Path:
    """The lock file for ``name``; a credential key is hashed, since it holds a URL."""
    stem = name if name == CONFIG_LOCK else hashlib.sha256(name.encode()).hexdigest()
    return config_dir() / "locks" / f"{stem}.lock"


@contextmanager
def locked(name: str) -> Iterator[None]:
    """Hold the exclusive lock ``name`` for the block; waits while another process holds it."""
    path = lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
