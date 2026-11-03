"""
The arguments and flags of one generated command, as plain data.

``databay.cli.build`` turns them into click parameters and ``databay.reference`` renders them into
``commands.md``, so ``--help`` and the reference list the same flags with the same text. Standard
library only: ``python/tools/gen_client.py`` reads it.

- A path parameter is a positional argument, in path order. The project's own path parameter is
  optional when nothing required follows it.
- A query or header parameter, or a body field, is ``--<name in kebab case>``. A list repeats the
  flag; an object takes JSON text.
- A secret field (``format: password``) is never a value flag: ``--<name>-env NAME`` names a
  variable that holds it and ``--<name>-stdin`` reads it from stdin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from databay.settings import OUTPUT_FORMATS

if TYPE_CHECKING:
    from databay.operation import Operation, Param

Role = Literal[
    "argument",
    "option",
    "secret_env",
    "secret_stdin",
    "project",
    "file",
    "yes",
    "wait",
    "timeout",
    "idempotency_key",
    "output",
    "profile",
    "verbose",
    "no_retry",
]

#: Flags the CLI adds itself; a parameter or body field that would take one of these names is a
#: generator error.
RESERVED_FLAGS = frozenset(
    {"project", "file", "yes", "wait", "timeout", "idempotency-key", "output", "profile", "verbose", "no-retry", "help"}
)


@dataclass(frozen=True, slots=True)
class Flag:
    role: Role
    #: The flag (``--source-id``) or the argument's metavar (``PIPE_UUID``).
    name: str
    dest: str
    help: str
    required: bool = False
    param: Param | None = None
    #: ``value``: takes a value; ``switch``: a bare flag; ``bool``: ``--x`` sends true and ``--no-x``
    #: false; ``multiple``: repeated, one item each.
    takes: Literal["value", "switch", "bool", "multiple", "argument"] = "value"
    choices: tuple[str, ...] = ()
    metavar: str | None = None


def _value_help(param: Param) -> str:
    text = param.description.strip().splitlines()[0] if param.description.strip() else ""
    if param.kind == "json":
        text = f"{text} (JSON)".strip()
    return text


def _argument(param: Param, *, required: bool) -> Flag:
    return Flag(
        "argument",
        param.name.upper(),
        param.identifier,
        _value_help(param),
        required=required,
        param=param,
        takes="argument",
    )


def _option(param: Param, *, role: Role = "option") -> list[Flag]:
    if param.secret:
        return [
            Flag(
                "secret_env",
                f"--{param.flag}-env",
                f"{param.identifier}_env",
                f"name of the environment variable that holds {param.name} (secret)",
                param=param,
                metavar="NAME",
            ),
            Flag(
                "secret_stdin",
                f"--{param.flag}-stdin",
                f"{param.identifier}_stdin",
                f"read {param.name} (secret) from stdin",
                param=param,
                takes="switch",
            ),
        ]
    takes = "multiple" if param.kind == "list" else "bool" if param.kind == "boolean" else "value"
    return [
        Flag(
            role,
            f"--{param.flag}",
            param.identifier,
            _value_help(param),
            required=param.required,
            param=param,
            takes=takes,
            choices=param.choices,
        )
    ]


def _project_path_optional(op: Operation) -> bool:
    location = op.project_location
    if location is None or location[0] != "path":
        return False
    names = [p.name for p in op.path_params]
    return names[-1] == location[1]


def command_flags(op: Operation, variant: str | None = None) -> list[Flag]:
    """Every argument and flag of ``op`` (of its body variant ``variant``), in the order ``--help`` shows them."""
    location = op.project_location
    flags: list[Flag] = []
    optional_last = _project_path_optional(op)
    for index, param in enumerate(op.path_params):
        is_project = location is not None and location == ("path", param.name)
        last = index == len(op.path_params) - 1
        flags.append(_argument(param, required=not (is_project and optional_last and last)))
    for param in op.params:
        if param.location == "path":
            continue
        if location is not None and location == (param.location, param.name):
            flags.append(
                Flag("option", f"--{param.flag}", param.identifier, _value_help(param), param=param, takes="value")
            )
            continue
        flags += _option(param)
    if op.body is not None:
        for param in op.body.fields_of(variant):
            if location is not None and location == ("body", param.name):
                flags.append(
                    Flag("option", f"--{param.flag}", param.identifier, _value_help(param), param=param, takes="value")
                )
                continue
            flags += _option(param)
        flags.append(Flag("file", "--file", "file", "read the whole body as JSON from a file, or - for stdin"))
    if op.project is not None:
        flags.append(Flag("project", "--project", "project", "the project to act in (default: the active project)"))
    if op.idempotent:
        flags.append(
            Flag(
                "idempotency_key",
                "--idempotency-key",
                "idempotency_key",
                "repeat an earlier call: the server answers it again and creates nothing (default: a new key)",
            )
        )
    if op.destructive:
        flags.append(Flag("yes", "--yes", "yes", "confirm: this removes or overwrites state", takes="switch"))
    if op.offers_wait:
        flags.append(Flag("wait", "--wait", "wait", "wait until it is done, polling its status", takes="switch"))
        flags.append(
            Flag("timeout", "--timeout", "timeout", "seconds --wait may take before exit 4 (resumable)", metavar="S")
        )
    flags += common_flags()
    return flags


def common_flags() -> list[Flag]:
    return [
        Flag("output", "--output", "output", "json, table or csv", choices=OUTPUT_FORMATS),
        Flag("profile", "--profile", "profile", "the profile to use (default: current_profile)"),
        Flag("verbose", "--verbose", "verbose", "log each request on stderr, secrets redacted", takes="switch"),
        Flag("no_retry", "--no-retry", "no_retry", "send each request once", takes="switch"),
    ]
