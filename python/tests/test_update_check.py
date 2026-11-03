"""The update notice: on a terminal only, once a day, never under CI or when turned off, never in the way."""

from __future__ import annotations

import io
import json
import os
import pty
import subprocess
import sys
import threading
import time

import pytest

from databay import update_check
from databay.update_check import UpdateCheck


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _cache(profile_env) -> dict:
    return json.loads((profile_env["state"] / "update-check.json").read_text(encoding="utf-8"))


def _write(profile_env, cache: dict) -> None:
    profile_env["state"].mkdir(parents=True, exist_ok=True)
    (profile_env["state"] / "update-check.json").write_text(json.dumps(cache), encoding="utf-8")


def _finished(check: UpdateCheck) -> None:
    if check._thread is not None:
        check._thread.join(timeout=5)


@pytest.mark.parametrize(
    ("environ", "stdout_tty", "stderr_tty", "on"),
    [
        ({}, True, True, True),
        ({"CI": "true"}, True, True, False),
        ({"DATABAY_NO_UPDATE_CHECK": "1"}, True, True, False),
        ({}, False, True, False),
        ({}, True, False, False),
    ],
    ids=["terminal", "ci", "turned-off", "stdout-captured", "stderr-captured"],
)
def test_the_check_runs_only_on_a_terminal_outside_ci(environ, stdout_tty, stderr_tty, on):
    stdout = Terminal() if stdout_tty else io.StringIO()
    stderr = Terminal() if stderr_tty else io.StringIO()

    assert update_check.enabled(environ, stdout, stderr) is on


def test_a_newer_release_is_announced_with_the_upgrade_command(profile_env):
    check = UpdateCheck(fetch=lambda: "9.0.0")
    stderr = Terminal()

    check.start()
    _finished(check)
    check.finish(stderr, None)

    assert stderr.getvalue() == "databay 9.0.0 is available: uv tool upgrade databay-cli\n"


def test_the_notice_and_the_check_happen_once_a_day(profile_env):
    calls: list[int] = []

    def fetch() -> str:
        calls.append(1)
        return "9.0.0"

    first, second = UpdateCheck(fetch=fetch), UpdateCheck(fetch=fetch)
    stderr = Terminal()
    for check in (first, second):
        check.start()
        _finished(check)
        check.finish(stderr, None)

    assert len(calls) == 1
    assert stderr.getvalue().count("is available") == 1


def test_a_failed_check_prints_nothing_and_is_an_event_under_verbose(profile_env):
    def fetch() -> str:
        raise OSError("offline")

    check = UpdateCheck(fetch=fetch)
    stderr = Terminal()
    events: list[tuple[str, dict]] = []

    check.start()
    _finished(check)
    check.finish(stderr, lambda name, fields: events.append((name, fields)))

    assert stderr.getvalue() == ""
    assert events == [("update_check_failed", {"error": "OSError"})]
    assert "checked_at" in _cache(profile_env)


def test_a_slow_pypi_holds_the_command_no_longer_than_the_check_timeout(profile_env):
    release = threading.Event()

    def fetch() -> str:
        release.wait(timeout=10)
        return "9.0.0"

    check = UpdateCheck(fetch=fetch, wait_s=0.2)
    stderr = Terminal()
    events: list[tuple[str, dict]] = []
    check.start()
    started = time.monotonic()
    check.finish(stderr, lambda name, fields: events.append((name, fields)))

    assert time.monotonic() - started < 2
    assert stderr.getvalue() == ""
    assert events == [("update_check_failed", {"error": "TimeoutError"})]
    release.set()
    _finished(check)


def test_a_check_that_finishes_after_the_notice_keeps_noticed_at(profile_env):
    state = profile_env["state"]
    state.mkdir(parents=True, exist_ok=True)
    _write(profile_env, {"package": "databay-cli", "checked_at": 0, "latest": "9.0.0"})
    release = threading.Event()

    def fetch() -> str:
        release.wait(timeout=10)
        return "9.0.1"

    check = UpdateCheck(fetch=fetch, wait_s=0.1)
    check.start()
    check.finish(Terminal(), None)
    release.set()
    _finished(check)

    cache = _cache(profile_env)
    assert (cache["latest"], "noticed_at" in cache) == ("9.0.1", True)


def test_the_same_or_an_older_release_says_nothing(profile_env):
    check = UpdateCheck(fetch=lambda: "0.0.1")
    stderr = Terminal()

    check.start()
    _finished(check)
    check.finish(stderr, None)

    assert stderr.getvalue() == ""


def test_a_cache_without_the_package_is_ignored_and_replaced_by_the_next_check(profile_env):
    _write(profile_env, {"checked_at": time.time(), "latest": "9.0.0", "noticed_at": 0})
    check = UpdateCheck(fetch=lambda: "0.0.1")
    stderr = Terminal()

    check.start()
    _finished(check)
    check.finish(stderr, None)

    assert stderr.getvalue() == ""
    cache = _cache(profile_env)
    assert (cache["package"], cache["latest"]) == ("databay-cli", "0.0.1")


def test_a_check_deletes_the_cache_named_for_the_package(profile_env):
    retired = profile_env["state"] / "update-check-databay-cli.json"
    retired.parent.mkdir(parents=True, exist_ok=True)
    retired.write_text(json.dumps({"checked_at": time.time(), "latest": "9.0.0"}), encoding="utf-8")
    check = UpdateCheck(fetch=lambda: "0.0.1")

    check.start()
    _finished(check)

    assert not retired.exists()
    assert _cache(profile_env)["latest"] == "0.0.1"


def _on_a_terminal(args: list[str], env: dict[str, str]) -> str:
    """Run the CLI with a terminal as stdin, stdout and stderr; return what the terminal showed."""
    leader, follower = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "databay", *args], stdin=follower, stdout=follower, stderr=follower, env=env
    )
    os.close(follower)
    seen = b""
    while True:
        try:
            chunk = os.read(leader, 4096)
        except OSError:
            break
        if not chunk:
            break
        seen += chunk
    process.wait(timeout=30)
    os.close(leader)
    return seen.decode(errors="replace")


@pytest.mark.parametrize(
    ("extra", "shown"), [({}, True), ({"CI": "1"}, False), ({"DATABAY_NO_UPDATE_CHECK": "1"}, False)]
)
def test_a_command_on_a_terminal_shows_the_cached_notice_unless_turned_off(profile_env, extra, shown):
    _write(profile_env, {"package": "databay-cli", "checked_at": time.time(), "latest": "9.0.0"})
    env = {k: v for k, v in os.environ.items() if k not in {"CI", "DATABAY_NO_UPDATE_CHECK"}}

    seen = _on_a_terminal(["profile", "list"], {**env, **extra})

    assert ("databay 9.0.0 is available: uv tool upgrade databay-cli" in seen) is shown, seen
