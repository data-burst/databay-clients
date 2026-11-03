"""
The ``databay`` entry point.

Each noun's commands live in their own generated module, imported only when that noun runs, so
``databay --help`` and ``databay pipe get --help`` load almost nothing. Every failure leaves through
``fail``: one error object on stderr and the exit code its code names.
"""

from __future__ import annotations

import importlib
import sys
import time
import traceback
from typing import TYPE_CHECKING, Any

import click

from databay.cli.handwritten import HANDWRITTEN
from databay.errors import DatabayError
from databay.platform_check import ensure_supported
from databay.settings import (
    ENV_CONFIG_DIR,
    ENV_NO_UPDATE_CHECK,
    ENV_OUTPUT,
    ENV_STATE_DIR,
    ENV_TOKEN,
    OUTPUT_RULE,
)
from databay.spec import API_VERSION, NOUNS
from databay.version import CLIENT_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx

    from databay.update_check import UpdateCheck

ROOT_HELP = f"""Databay from the command line: databay <noun> <verb> [ARGS] [OPTIONS].

{OUTPUT_RULE} stdout carries only the result; progress, hints and errors go to stderr.

\b
Environment:
  {ENV_TOKEN}       the access token, for a profile whose auth is "env";
                       set under a login or api_key profile it is an error
  {ENV_OUTPUT}      json, table or csv
  {ENV_CONFIG_DIR}  where config.toml lives (default: the platform's config dir)
  {ENV_STATE_DIR}   where caches and crash logs go (default: the platform's state dir)
  {ENV_NO_UPDATE_CHECK}=1  no daily check for a newer release (also off under CI)
  NO_COLOR             no colors in tables

\b
Exit codes: 0 ok, 1 refused, 2 usage, 3 profile or token, 4 still pending,
5 wait failed, 6 transient, 7 version mismatch, 8 client outdated or bug,
9 not ready. A failure prints {{"error": {{"code", "message", "hint", "next",
"recovery", "context"}}}} on stderr when captured."""


class RootGroup(click.Group):
    """Lists every noun from the generated index; imports a noun's module only to run it."""

    def list_commands(self, ctx: click.Context) -> list[str]:  # noqa: ARG002 — click's signature
        return sorted(set(NOUNS) | {name for name in HANDWRITTEN if " " not in name})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:  # noqa: ARG002
        if cmd_name in HANDWRITTEN:
            return _handwritten(cmd_name)
        if cmd_name not in NOUNS:
            return None
        module, _ = NOUNS[cmd_name]
        group: click.Group = importlib.import_module(f"databay.cli.commands.{module}").group
        for verb in _handwritten_verbs(cmd_name):
            group.add_command(_handwritten(f"{cmd_name} {verb}"), verb)
        return group

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:  # noqa: ARG002
        rows = [(noun, ", ".join(sorted([*verbs, *_handwritten_verbs(noun)]))) for noun, (_, verbs) in NOUNS.items()]
        rows += [(name, summary) for name, (_, _, summary) in HANDWRITTEN.items() if " " not in name]
        with formatter.section("Commands"):
            formatter.write_dl(sorted(rows))


def _handwritten(command: str) -> click.Command:
    module, attribute, _ = HANDWRITTEN[command]
    return getattr(importlib.import_module(module), attribute)


def _handwritten_verbs(noun: str) -> list[str]:
    """The hand-written verbs of a generated noun (``project use``)."""
    return [verb for name in HANDWRITTEN for head, _, verb in [name.partition(" ")] if verb and head == noun]


@click.group(cls=RootGroup, help=ROOT_HELP, context_settings={"max_content_width": 110})
@click.version_option(f"{CLIENT_VERSION} (API {API_VERSION})", prog_name="databay", message="%(prog)s %(version)s")
def root() -> None:
    pass


def _crash_log(exc: BaseException) -> str | None:
    from databay.paths import state_dir  # noqa: PLC0415

    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"crash-{time.strftime('%Y%m%dT%H%M%S')}.log"
        path.write_text("".join(traceback.format_exception(exc)), encoding="utf-8")
    except OSError:
        return None
    return str(path)


def fail(error: DatabayError, state: dict[str, Any]) -> int:
    from databay.cli.console import Console  # noqa: PLC0415

    console: Console = state.get("console") or Console()
    body = error.to_dict()
    body["context"] = {
        "cli_version": CLIENT_VERSION,
        "api_version": API_VERSION,
        **dict.fromkeys(("server", "profile", "project", "request_id")),
        **state.get("context", {}),
    }
    console.error(body)
    return error.exit_code


def main(argv: Sequence[str] | None = None, *, http: httpx.Client | None = None) -> int:
    """
    Run the CLI and return its exit code; the console script exits with it.

    ``http`` is the httpx client every request goes through; tests pass one on ``httpx.MockTransport``.
    """
    state: dict[str, Any] = {"http": http}
    update = _start_update_check()
    try:
        ensure_supported()
        root.main(args=list(argv) if argv is not None else None, prog_name="databay", standalone_mode=False, obj=state)
    except DatabayError as exc:
        code = fail(exc, state)
    except click.exceptions.NoArgsIsHelpError as exc:
        click.echo(exc.ctx.get_help() if exc.ctx is not None else exc.format_message())
        code = 0
    except click.exceptions.Abort:
        from databay.errors import DatabayCancelled  # noqa: PLC0415

        if state.get("sent"):
            key = state.get("idempotency_key")
            error = DatabayCancelled(
                "interrupted after the request was sent",
                code="interrupted",
                details={"idempotency_key": key} if key else {},
                next_command=state.get("next"),
            )
        elif state.get("reading"):
            error = DatabayCancelled(
                "stopped while reading; nothing was changed", code="stopped", next_command=state.get("next")
            )
        else:
            error = DatabayCancelled("cancelled; nothing was sent")
        code = fail(error, state)
    except click.UsageError as exc:
        from databay.errors import DatabayUsageError  # noqa: PLC0415

        hint = exc.ctx.get_usage() if exc.ctx is not None else None
        code = fail(DatabayUsageError(exc.format_message(), hint=hint), state)
    except click.ClickException as exc:
        from databay.errors import DatabayUsageError  # noqa: PLC0415

        code = fail(DatabayUsageError(exc.format_message()), state)
    except Exception as exc:  # noqa: BLE001 — the last line: every crash leaves as internal_error
        path = _crash_log(exc)
        if state.get("console") is not None and state["console"].verbose:
            traceback.print_exception(exc, file=sys.stderr)
        error = DatabayError(
            f"databay crashed: {type(exc).__name__}" + (f"; the trace is in {path}" if path else ""),
            details={"crash_log": path},
        )
        code = fail(error, state)
    else:
        code = 0
    if update is not None:
        console = state.get("console")
        update.finish(sys.stderr, console.event if console is not None and console.verbose else None)
    return code


def _start_update_check() -> UpdateCheck | None:
    import os  # noqa: PLC0415

    from databay import update_check  # noqa: PLC0415

    if not update_check.enabled(dict(os.environ), sys.stdout, sys.stderr):
        return None
    check = update_check.UpdateCheck()
    check.start()
    return check
