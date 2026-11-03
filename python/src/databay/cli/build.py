"""
Click commands from generated ``Operation`` data.

Each operation becomes ``databay <noun> <verb>``; a discriminated body, or a body's discriminated
property, becomes one subcommand per variant (``databay source precheck postgres``, ``databay source
create postgres``). The flags come from ``databay.cli.flags`` and the
help text from ``databay.reference``, the same functions ``commands.md`` is rendered with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import click

from databay.cli.flags import Flag, command_flags
from databay.reference import click_help

if TYPE_CHECKING:
    from databay.operation import Operation

CLICK_TYPES: dict[str, Any] = {"integer": click.INT, "number": click.FLOAT, "string": click.STRING}


def _click_type(flag: Flag) -> Any:  # noqa: ANN401 — a click ParamType
    if flag.choices:
        return click.Choice(flag.choices)
    if flag.role == "timeout":
        return click.FloatRange(min=0, min_open=True)
    param = flag.param
    if param is None:
        return click.STRING
    kind = param.item_kind if param.kind == "list" else param.kind
    return CLICK_TYPES.get(kind or "string", click.STRING)


def click_parameter(flag: Flag) -> click.Parameter:
    if flag.takes == "argument":
        return click.Argument([flag.dest], required=flag.required, type=_click_type(flag), metavar=flag.name)
    help_text = flag.help + (" [required]" if flag.required else "")
    if flag.takes == "switch":
        return click.Option([flag.name, flag.dest], is_flag=True, default=False, help=help_text)
    if flag.takes == "bool":
        negative = "--no-" + flag.name.removeprefix("--")
        return click.Option([f"{flag.name}/{negative}", flag.dest], default=None, help=help_text)
    return click.Option(
        [flag.name, flag.dest],
        type=_click_type(flag),
        multiple=flag.takes == "multiple",
        default=None,
        help=help_text,
        metavar=flag.metavar,
    )


def operation_command(op: Operation, variant: str | None = None) -> click.Command:
    flags = command_flags(op, variant)

    @click.pass_context
    def callback(ctx: click.Context, **values: Any) -> None:  # noqa: ANN401
        from databay.cli.runner import run  # noqa: PLC0415 — the runner loads httpx and pydantic

        run(ctx, op, variant, flags, values)

    name = variant if variant is not None else op.verb
    return click.Command(
        name,
        callback=callback,
        params=[click_parameter(flag) for flag in flags],
        help=click_help(op, variant),
        short_help=op.summary,
        no_args_is_help=False,
    )


def verb_command(op: Operation) -> click.Command:
    if op.body is None or op.body.discriminator is None:
        return operation_command(op)
    group = click.Group(op.verb, help=click_help(op), short_help=op.summary)
    for key, _, _ in op.body.variants:
        group.add_command(operation_command(op, key))
    return group


def noun_group(noun: str, operations: tuple[Operation, ...]) -> click.Group:
    group = click.Group(noun, help=f"The {noun} commands.")
    for op in operations:
        group.add_command(verb_command(op))
    return group
