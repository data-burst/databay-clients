"""The client runs on Linux and macOS; anything else stops with ``unsupported_platform``."""

from __future__ import annotations

import sys

from databay.errors import DatabayUsageError

SUPPORTED_PLATFORMS = ("linux", "darwin")


def ensure_supported(platform: str = sys.platform) -> None:
    if platform.startswith(SUPPORTED_PLATFORMS):
        return
    hint = "Use WSL on Windows." if platform in {"win32", "cygwin"} else "Use Linux or macOS."
    raise DatabayUsageError(
        f"databay runs on Linux and macOS, not {platform}. {hint}",
        code="unsupported_platform",
        details={"platform": platform},
    )
