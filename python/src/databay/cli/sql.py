"""``databay sql``: the curated command for ``POST /api/configstore/sql/execute``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from databay.cli.common import common_options, console_for

SQL_HELP = """Run one read-only statement and print its rows.

\b
The statement is the argument, or the text of --file PATH (--file - reads
stdin), or stdin when neither is given and stdin is not a terminal.
\b
JSON prints the server's answer (columns, rows, row_count, truncated,
warnings); a table or CSV prints the rows under their column names.

\b
Example:
  databay sql --db <db_uuid> "select count(*) from analytics.orders\""""


def _statement(argument: str | None, path: str | None) -> str:
    from databay.errors import DatabayUsageError  # noqa: PLC0415

    if argument is not None and path is not None:
        raise DatabayUsageError("pass the statement as the argument or with --file, not both")
    if argument is not None:
        text = argument
    elif path == "-" or (path is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
    elif path is not None:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise DatabayUsageError(f"--file {path}: {exc.strerror}") from None
    else:
        raise DatabayUsageError("pass the statement as the argument, with --file PATH, or on stdin")
    if not text.strip():
        raise DatabayUsageError("the statement is empty")
    return text.strip()


@click.command("sql", help=SQL_HELP, short_help="Run one read-only SQL statement")
@click.argument("statement", required=False)
@click.option("--file", "file", default=None, metavar="PATH", help="read the statement from a file, or - for stdin")
@click.option(
    "--db", "db", default=None, metavar="DB_UUID", help="the warehouse to run against (default: the org's default)"
)
@click.option("--project", default=None, help="the project to act in (default: the active project)")
@common_options
@click.pass_context
def sql(
    ctx: click.Context,
    statement: str | None,
    file: str | None,
    db: str | None,
    project: str | None,
    **values: Any,  # noqa: ANN401
) -> None:
    from databay.cli.output import emit_grid  # noqa: PLC0415
    from databay.cli.runner import open_session  # noqa: PLC0415
    from databay.invoke import CallValues, resolve_project  # noqa: PLC0415
    from databay.operations.sql import SQL_EXECUTE  # noqa: PLC0415

    console, state = console_for(ctx, values)
    query = _statement(statement, file)
    body: dict[str, Any] = {"query": query}
    if db is not None:
        body["db_uuid"] = db
    call = CallValues(body=body, has_body=True, project_flag=project)
    session, chosen = open_session(values, console, state)
    chosen_project = resolve_project(SQL_EXECUTE, call, session.active_project)
    state["context"] = {
        "server": chosen.server,
        "profile": chosen.name,
        "project": chosen_project,
        "request_id": session.transport.request_id,
    }
    state["reading"] = True
    try:
        result = session.call(SQL_EXECUTE, call)
    finally:
        session.transport.close()
    state["context"]["request_id"] = result.request_id
    answer = result.parsed
    if answer.truncated:
        console.notice(
            "truncated",
            f"the server returned the first {answer.row_count} rows",
            only_on_terminal=console.output == "json",
        )
    for warning in answer.warnings or []:
        console.notice("warning", f"{warning.code}: {warning.message}", only_on_terminal=console.output == "json")
    envelope: dict[str, Any] = {"result": result.raw}
    if result.project is not None:
        envelope["project"] = result.project
    emit_grid(console, envelope, list(answer.columns), [list(row) for row in answer.rows])
