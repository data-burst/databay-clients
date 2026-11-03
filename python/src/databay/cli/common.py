"""What every hand-written command shares with the generated ones: the common flags and the console."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from databay.cli.build import click_parameter
from databay.cli.flags import common_flags

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import click
    import httpx

    from databay.cli.console import Console


def common_options(command: Callable[..., Any], *, profile_help: str | None = None) -> Callable[..., Any]:
    """
    Add ``--output``, ``--profile``, ``--verbose`` and ``--no-retry``, built as the generated commands build them.

    ``profile_help`` replaces the help of ``--profile`` for a command that writes the profile (``login``).
    """
    flags = [replace(f, help=profile_help) if profile_help and f.role == "profile" else f for f in common_flags()]
    params = getattr(command, "__click_params__", [])
    params.extend(click_parameter(flag) for flag in reversed(flags))
    command.__click_params__ = params  # type: ignore[attr-defined]
    return command


def login_options(command: Callable[..., Any]) -> Callable[..., Any]:
    from databay.settings import DEFAULT_PROFILE  # noqa: PLC0415

    return common_options(command, profile_help=f"the profile to create or update (default: {DEFAULT_PROFILE})")


def console_for(ctx: click.Context, values: dict[str, Any]) -> tuple[Console, dict[str, Any]]:
    """The command's console, registered in the CLI state so a failure is printed through it."""
    from databay.cli.console import Console, output_format  # noqa: PLC0415

    state: dict[str, Any] = ctx.ensure_object(dict)
    console = Console(output=output_format(values.get("output"), sys.stdout), verbose=bool(values.get("verbose")))
    state["console"] = console
    return console, state


@contextmanager
def http_client(state: dict[str, Any]) -> Iterator[httpx.Client]:
    """The httpx client requests go through: the one ``main`` was handed (tests), else a new one closed after."""
    if state.get("http") is not None:
        yield state["http"]
        return
    from databay.settings import SETTINGS  # noqa: PLC0415
    from databay.transport import new_http_client  # noqa: PLC0415

    with new_http_client(SETTINGS) as client:
        yield client
