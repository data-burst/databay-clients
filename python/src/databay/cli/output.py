"""
Printing a command's result.

JSON is one envelope: ``{"result": <the server's answer>}``, plus ``project`` when the command
acted in one, ``wait`` after ``--wait``, ``idempotency_key`` on a create and ``next`` when there is
an obvious next command. A table or CSV prints the answer alone: a list of objects as rows (the
operation's declared columns, else every key), an object as one row per field.
"""

from __future__ import annotations

import csv
import json
from typing import TYPE_CHECKING, Any

from databay.errors import DatabayResponseInvalid
from databay.invoke import pointer

if TYPE_CHECKING:
    from databay.cli.console import Console

MAX_CELL = 80


def _text(value: Any) -> str:  # noqa: ANN401
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _cell(value: Any) -> str:  # noqa: ANN401
    """A table cell, cut to ``MAX_CELL`` characters; CSV keeps the whole value."""
    text = _text(value)
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def _rows(result: Any, columns: tuple[str, ...], cell: Any = _cell) -> tuple[list[str], list[list[str]]]:  # noqa: ANN401
    if isinstance(result, list) and all(isinstance(item, dict) for item in result):
        header = list(columns)
        if not header:
            for item in result:
                header += [key for key in item if key not in header]
        return header, [[cell(item.get(key)) for key in header] for item in result]
    if isinstance(result, dict):
        return ["field", "value"], [[key, cell(value)] for key, value in result.items()]
    if isinstance(result, list):
        return ["value"], [[cell(item)] for item in result]
    return ["value"], [[cell(result)]]


def _csv_rows(result: Any, columns: tuple[str, ...]) -> tuple[list[str], list[list[str]]]:  # noqa: ANN401
    if isinstance(result, dict):
        header = list(columns) or list(result)
        return header, [[_text(result.get(key)) for key in header]]
    return _rows(result, columns, _text)


def emit(console: Console, envelope: dict[str, Any], columns: tuple[str, ...], rows: str | None = None) -> None:
    """
    Print ``envelope`` in the console's format.

    ``rows`` is the JSON pointer of the list a table or CSV shows when the answer is an object
    holding it (``/keys``); a table or CSV reports ``idempotency_key`` and ``next`` on stderr.
    """
    _write(console, envelope, columns, rows)
    for name in ("idempotency_key", "next"):
        if envelope.get(name):
            console.notice(name, envelope[name], only_on_terminal=console.output == "json")


def emit_grid(console: Console, envelope: dict[str, Any], header: list[str], rows: list[list[Any]]) -> None:
    """
    Print an answer that carries its own grid of ``header`` and ``rows`` (``sql``).

    JSON prints ``envelope``; a table or CSV prints the grid as it is, so repeated column names stay apart.
    """
    out = console.stdout
    if console.output == "json":
        out.write(json.dumps(envelope, indent=2 if out.isatty() else None, default=str) + "\n")
        return
    if console.output == "csv":
        writer = csv.writer(out)
        writer.writerow(header)
        writer.writerows([[_text(value) for value in row] for row in rows])
        return
    if envelope.get("project"):
        out.write(f"project: {envelope['project']}\n")
    from rich.console import Console as RichConsole  # noqa: PLC0415 — tables only
    from rich.table import Table  # noqa: PLC0415

    table = Table(*header, show_lines=False)
    for row in rows:
        table.add_row(*[_cell(value) for value in row])
    RichConsole(file=out, no_color=not console.color, soft_wrap=False, markup=False, highlight=False).print(table)


def _write(console: Console, envelope: dict[str, Any], columns: tuple[str, ...], rows: str | None) -> None:
    out = console.stdout
    result = envelope["result"]
    if console.output == "json":
        indent = 2 if out.isatty() else None
        out.write(json.dumps(envelope, indent=indent, default=str) + "\n")
        return
    if isinstance(result, str):
        out.write(result if result.endswith("\n") else result + "\n")
        return
    if rows is not None and isinstance(result, dict):
        try:
            result = pointer(result, rows)
        except (KeyError, IndexError, ValueError):
            raise DatabayResponseInvalid(f"the answer has no {rows}, the list its columns describe") from None
    if console.output == "csv":
        header, cells = _csv_rows(result, columns)
        writer = csv.writer(out)
        writer.writerow(header)
        writer.writerows(cells)
        return
    if envelope.get("project"):
        out.write(f"project: {envelope['project']}\n")
    if "wait" in envelope:
        wait = envelope["wait"]
        reason = f"{wait['reason']}, " if wait.get("reason") else ""
        out.write(f"wait: {wait['outcome']} ({reason}{wait['state']}, {wait['elapsed_s']}s)\n")
    if result is None:
        out.write("done\n")
        return
    from rich.console import Console as RichConsole  # noqa: PLC0415 — tables only
    from rich.table import Table  # noqa: PLC0415

    header, cells = _rows(result, columns)
    table = Table(*header, show_lines=False)
    for row in cells:
        table.add_row(*row)
    RichConsole(file=out, no_color=not console.color, soft_wrap=False, markup=False, highlight=False).print(table)
