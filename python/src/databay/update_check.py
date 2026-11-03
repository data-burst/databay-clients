"""
The update notice: ``databay <new> is available: uv tool upgrade databay-cli`` on stderr.

Only on a terminal (stdout and stderr), never under ``$CI``, and off with
``DATABAY_NO_UPDATE_CHECK=1``. PyPI is asked at most once per ``update_check_interval_s``, in a
background thread that runs beside the command; when the command is done it waits for the thread
at most ``update_check_timeout_s`` more. The answer is cached in the state dir, and the notice is
printed from that cache at most once per interval. A failed or unfinished check prints nothing,
except the ``update_check_failed`` event under ``--verbose``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

from databay.paths import state_dir
from databay.settings import ENV_CI, ENV_NO_UPDATE_CHECK, SETTINGS
from databay.version import CLIENT_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PACKAGE = "databay-cli"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
UPGRADE_COMMAND = f"uv tool upgrade {PACKAGE}"
CACHE_NAME = "update-check.json"
#: A cache file named for the package, which some installs still have; every cache write deletes it.
RETIRED_CACHE_NAME = "update-check-databay-cli.json"


def enabled(environ: dict[str, str], stdout: TextIO, stderr: TextIO) -> bool:
    if environ.get(ENV_NO_UPDATE_CHECK) == "1" or environ.get(ENV_CI):
        return False
    return stdout.isatty() and stderr.isatty()


def _release(text: str) -> tuple[int, ...]:
    """``1.2.3`` as ``(1, 2, 3)``; a pre-release or anything else non-numeric is a ``ValueError``."""
    return tuple(int(part) for part in text.split("."))


def _cache_file() -> Path:
    return state_dir() / CACHE_NAME


def _read_cache() -> dict[str, Any]:
    """
    The cached answer about ``PACKAGE``; nothing when the cache is about another package.

    An ``update-check.json`` without ``package`` holds an answer about the unrelated ``databay`` on
    PyPI. It is ignored here, and the next check replaces it.
    """
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("package") == PACKAGE else {}


def _write_cache(data: dict[str, Any]) -> None:
    path = _cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({**data, "package": PACKAGE}), encoding="utf-8")
    tmp.replace(path)
    (path.parent / RETIRED_CACHE_NAME).unlink(missing_ok=True)


def fetch_latest() -> str:
    """The newest release on PyPI."""
    import httpx  # noqa: PLC0415

    response = httpx.get(PYPI_URL, timeout=SETTINGS.update_check_timeout_s)
    response.raise_for_status()
    version = response.json()["info"]["version"]
    if not isinstance(version, str):
        raise TypeError("info.version is not a string")
    return version


@dataclass
class UpdateCheck:
    fetch: Callable[[], str] = fetch_latest
    now: Callable[[], float] = time.time
    #: How long ``finish`` waits for a check still running.
    wait_s: float = SETTINGS.update_check_timeout_s
    #: The exception a check in this process raised, reported under ``--verbose``.
    failure: BaseException | None = None
    _thread: threading.Thread | None = field(default=None, repr=False)

    def start(self) -> None:
        """Ask PyPI in the background when the cached answer is older than the interval."""
        checked = _read_cache().get("checked_at")
        if isinstance(checked, int | float) and self.now() - checked < SETTINGS.update_check_interval_s:
            return
        self._thread = threading.Thread(target=self._check, name="databay-update-check", daemon=True)
        self._thread.start()

    def _check(self) -> None:
        latest: str | None = None
        try:
            latest = self.fetch()
            _release(latest)
        except Exception as exc:  # noqa: BLE001 — any failure only skips the notice
            self.failure = exc
            latest = None
        # Read just before writing, so a notice shown meanwhile keeps its noticed_at.
        cache = _read_cache()
        if latest is not None:
            cache["latest"] = latest
        cache["checked_at"] = self.now()
        try:
            _write_cache(cache)
        except OSError as exc:
            self.failure = exc

    def finish(self, stderr: TextIO, event: Callable[[str, dict[str, Any]], None] | None) -> None:
        """Print the notice from the cache when it names a newer release and none was shown this interval."""
        if self._thread is not None:
            self._thread.join(timeout=self.wait_s)
            if self._thread.is_alive() and self.failure is None:
                self.failure = TimeoutError("the PyPI check did not finish")
        self._notice(stderr)
        if self.failure is not None and event is not None:
            event("update_check_failed", {"error": type(self.failure).__name__})

    def _notice(self, stderr: TextIO) -> None:
        cache = _read_cache()
        latest, noticed = cache.get("latest"), cache.get("noticed_at")
        if not isinstance(latest, str):
            return
        try:
            newer = _release(latest) > _release(CLIENT_VERSION)
        except ValueError:
            return
        if not newer:
            return
        if isinstance(noticed, int | float) and self.now() - noticed < SETTINGS.update_check_interval_s:
            return
        stderr.write(f"databay {latest} is available: {UPGRADE_COMMAND}\n")
        cache["noticed_at"] = self.now()
        try:
            _write_cache(cache)
        except OSError as exc:
            self.failure = exc
