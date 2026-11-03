"""
Where the CLI writes: the result on stdout, everything else on stderr.

stdout carries only the result, so piping it never mixes in progress or hints. On stderr, a
terminal gets short human lines and captured output gets one JSON object per line.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from databay.errors import DatabayUsageError
from databay.settings import ENV_NO_COLOR, ENV_OUTPUT, OUTPUT_FORMATS, OutputFormat


def output_format(flag: str | None, stdout: TextIO) -> OutputFormat:
    """``--output``, else ``$DATABAY_OUTPUT``, else a table on a terminal and JSON when captured."""
    return output_rule(flag, stdout)[0]


def output_rule(flag: str | None, stdout: TextIO) -> tuple[OutputFormat, str]:
    """The output format and which part of the output rule chose it (``databay status`` prints it)."""
    if flag is not None:
        return flag, "--output"  # type: ignore[return-value] — click already checked the choice
    env = os.environ.get(ENV_OUTPUT)
    if env:
        if env not in OUTPUT_FORMATS:
            raise DatabayUsageError(f"{ENV_OUTPUT}={env!r} is not one of {', '.join(OUTPUT_FORMATS)}")
        return env, ENV_OUTPUT  # type: ignore[return-value]
    if stdout.isatty():
        return "table", "stdout is a terminal"
    return "json", "stdout is captured"


@dataclass
class Console:
    output: OutputFormat = "json"
    verbose: bool = False
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    _live_line: bool = False
    #: What ``notice`` already showed on a terminal, so the error lines do not repeat it.
    _noticed: dict[str, str] = field(default_factory=dict)

    @property
    def stderr_tty(self) -> bool:
        return self.stderr.isatty()

    @property
    def color(self) -> bool:
        return self.stdout.isatty() and not os.environ.get(ENV_NO_COLOR)

    def _end_live_line(self) -> None:
        if self._live_line:
            self.stderr.write("\n")
            self._live_line = False

    def event(self, name: str, fields: dict[str, Any]) -> None:
        """One stderr event; ``request`` and ``response`` events only under ``--verbose``."""
        if name in {"request", "response", "cache_invalid", "cache_write_failed"} and not self.verbose:
            return
        if not self.stderr_tty:
            self.stderr.write(json.dumps({"event": name, **fields}, default=str) + "\n")
            self.stderr.flush()
            return
        self._end_live_line()
        details = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        self.stderr.write(f"{name}: {details}\n")
        self.stderr.flush()

    def progress(self, state: str | None, elapsed_s: float, operation: str) -> None:
        """A wait's state: a live line on a terminal, one ``wait`` event per state change when captured."""
        if not self.stderr_tty:
            self.event("wait", {"state": state, "elapsed_s": round(elapsed_s, 1), "status_operation": operation})
            return
        self.stderr.write(f"\rwaiting: {state} ({round(elapsed_s)}s)\033[K")
        self.stderr.flush()
        self._live_line = True

    def notice(self, name: str, value: str, *, only_on_terminal: bool) -> None:
        """
        A value the result does not carry, such as ``next``: ``name: value`` on a terminal, an event when captured.

        ``only_on_terminal`` is for JSON output, whose envelope already holds the value.
        """
        if self.stderr_tty:
            self._end_live_line()
            self.stderr.write(f"{name}: {value}\n")
            self._noticed[name] = value
            return
        if not only_on_terminal:
            self.event(name, {"value": value})

    def inform(self, name: str, fields: dict[str, Any], text: str) -> None:
        """Something the user must act on: ``text`` on a terminal, the ``name`` event with ``fields`` when captured."""
        if not self.stderr_tty:
            self.event(name, fields)
            return
        self._end_live_line()
        self.stderr.write(text + "\n")
        self.stderr.flush()

    def error(self, body: dict[str, Any]) -> None:
        self._end_live_line()
        if not self.stderr_tty:
            self.stderr.write(json.dumps({"error": body}, default=str) + "\n")
            self.stderr.flush()
            return
        self.stderr.write(f"error: {body['code']}: {body['message']}\n")
        if body.get("hint"):
            self.stderr.write(f"hint: {body['hint']}\n")
        if body.get("next") and self._noticed.get("next") != body["next"]:
            self.stderr.write(f"next: {body['next']}\n")
        self.stderr.flush()
