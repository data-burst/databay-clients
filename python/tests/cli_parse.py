"""Parse a written ``databay …`` command line with the real CLI parser, without running it."""

from __future__ import annotations

import shlex

import click

from databay.cli.main import root


def _placeholder(param: click.Parameter, value: str) -> str:
    """A ``<placeholder>`` stands for a value the reader fills in: give the parser one of the right type."""
    if not value.startswith("<"):
        return value
    if isinstance(param.type, click.Choice):
        return str(param.type.choices[0])
    if isinstance(param.type, click.types.IntParamType | click.types.FloatParamType):
        return "1"
    return value


def _fill(command: click.Command, words: list[str]) -> list[str]:
    options = {name: p for p in command.params if isinstance(p, click.Option) for name in p.opts}
    filled = list(words)
    for index, word in enumerate(filled[:-1]):
        param = options.get(word)
        if param is not None and not param.is_flag:
            filled[index + 1] = _placeholder(param, filled[index + 1])
    return filled


def parse(line: str) -> click.Context:
    """The context of the command ``line`` names; a word or flag the CLI lacks raises ``click.UsageError``."""
    words = shlex.split(line)
    assert words[0] == "databay"
    ctx = root.make_context("databay", words[1:])
    command: click.Command = root
    while isinstance(command, click.Group):
        rest = [*getattr(ctx, "_protected_args", []), *ctx.args]
        name, sub, rest = command.resolve_command(ctx, rest)
        assert sub is not None, f"no command {name}"
        command = sub
        ctx = command.make_context(name, _fill(command, rest), parent=ctx)
    return ctx


def every_option_name() -> set[str]:
    """Every ``--flag`` any command of the CLI takes, generated or hand-written."""
    names: set[str] = set()
    ctx = click.Context(root)

    def visit(command: click.Command) -> None:
        names.update(opt for p in command.params if isinstance(p, click.Option) for opt in [*p.opts, *p.secondary_opts])
        if isinstance(command, click.Group):
            for name in command.list_commands(ctx):
                sub = command.get_command(ctx, name)
                assert sub is not None, name
                visit(sub)

    visit(root)
    return names
