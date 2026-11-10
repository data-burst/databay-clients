"""``databay metrics describe`` and ``databay metrics query``: the curated commands of the metrics API."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import click

from databay.cli.common import common_options, console_for

if TYPE_CHECKING:
    from databay.cli.console import Console
    from databay.invoke import CallValues, Result
    from databay.operation import Operation

#: Who ran the query, for the server's per-caller count; the CLI has no agent mode, so always ``cli``.
CALLER_HEADER = "X-Databay-Caller"
#: ``--where`` operators; ``!=``, ``>=`` and ``<=`` come before ``=``, ``>`` and ``<`` so the regex reads them whole.
WHERE_OPS = {"!=": "notEquals", ">=": "gte", "<=": "lte", "=": "equals", ">": "gt", "<": "lt"}
_WHERE = re.compile(r"\s*([a-z][a-z0-9_]*)\s*(" + "|".join(map(re.escape, WHERE_OPS)) + r")(.+)", re.DOTALL)
_GRAINS = ("day", "week", "month", "quarter", "year")
_PROJECT_HELP = "the project to act in (default: the active project)"


def parse_where(text: str) -> dict[str, Any]:
    """
    One ``--where`` as a filter: ``member<op>value`` with ``=``, ``!=``, ``>``, ``>=``, ``<`` or ``<=``.

    The value is everything after the operator, kept whole (commas and spaces included).
    """
    from databay.errors import DatabayUsageError  # noqa: PLC0415

    match = _WHERE.fullmatch(text)
    if match is None:
        raise DatabayUsageError(
            f"--where {text!r}: write <dimension><op><value>, op one of {', '.join(WHERE_OPS)} (channel=paid)"
        )
    member, symbol, value = match.groups()
    return {"member": member, "op": WHERE_OPS[symbol], "values": [value]}


def _call(ctx: click.Context, values: dict[str, Any], op: Operation, call: CallValues) -> tuple[Console, Result]:
    from databay.cli.runner import open_session  # noqa: PLC0415
    from databay.invoke import resolve_project  # noqa: PLC0415

    console, state = console_for(ctx, values)
    session, chosen = open_session(values, console, state)
    state["context"] = {
        "server": chosen.server,
        "profile": chosen.name,
        "project": resolve_project(op, call, session.active_project),
        "request_id": session.transport.request_id,
    }
    state["reading"] = True
    try:
        result = session.call(op, call)
    finally:
        session.transport.close()
    state["context"]["request_id"] = result.request_id
    return console, result


def _envelope(result: Result) -> dict[str, Any]:
    envelope: dict[str, Any] = {"result": result.raw}
    if result.project is not None:
        envelope["project"] = result.project
    return envelope


DESCRIBE_HELP = """Describe one metric: its dimensions, time grains, filter operators, warehouses and state.

\b
Example:
  databay metrics describe net_sales"""


@click.command("describe", help=DESCRIBE_HELP, short_help="Describe one metric you can query")
@click.argument("metric", metavar="METRIC")
@click.option("--project", default=None, help=_PROJECT_HELP)
@common_options
@click.pass_context
def describe(ctx: click.Context, metric: str, project: str | None, **values: Any) -> None:  # noqa: ANN401
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.invoke import CallValues  # noqa: PLC0415
    from databay.operations.metrics import METRICS_DESCRIBE  # noqa: PLC0415

    call = CallValues(path={"metric": metric}, project_flag=project)
    console, result = _call(ctx, values, METRICS_DESCRIBE, call)
    emit(console, _envelope(result), ())


QUERY_HELP = """Query one metric, grouped by dimensions and a time grain.

\b
A table or CSV prints the rows under their columns; JSON prints the whole
answer (columns, rows, row_count, truncated, answer_path, model_version,
warehouse, explain). --explain adds the metric's definition, the SQL it
compiled to and its lineage; --dry-run answers that without running it.
\b
--where member=value filters on a dimension; it also takes !=, >, >=, <
and <=, and repeats (each must hold). The value is taken whole.
\b
Examples:
  databay metrics query net_sales --grain day --last 30d
  databay metrics query channel_sessions --by channel --grain week --last 90d --where channel=Direct
  databay metrics query net_sales --grain day --last 30d --explain"""


@click.command("query", help=QUERY_HELP, short_help="Query one metric by dimensions and time grain")
@click.argument("metric", metavar="METRIC")
@click.option("--by", "by", multiple=True, metavar="DIMENSION", help="group by this dimension; repeat for more")
@click.option("--grain", type=click.Choice(_GRAINS), default=None, help="group by the metric's time at this grain")
@click.option("--last", default=None, metavar="ND", help="the N UTC days that end today, such as 30d")
@click.option("--from", "start", default=None, metavar="YYYY-MM-DD", help="first day (with --to)")
@click.option("--to", "end", default=None, metavar="YYYY-MM-DD", help="last day, inclusive (with --from)")
@click.option("--where", "where", multiple=True, metavar="FILTER", help="member=value (or != > >= < <=); repeat")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="most rows to answer")
@click.option("--warehouse", default=None, help="the warehouse, when the metric is in several")
@click.option("--model-version", "model_version", type=click.IntRange(min=1), default=None, help="pin a model version")
@click.option("--explain", is_flag=True, default=False, help="add the definition, compiled SQL and lineage")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="answer the explain without running it")
@click.option("--project", default=None, help=_PROJECT_HELP)
@common_options
@click.pass_context
def query(
    ctx: click.Context,
    metric: str,
    by: tuple[str, ...],
    grain: str | None,
    last: str | None,
    start: str | None,
    end: str | None,
    where: tuple[str, ...],
    limit: int | None,
    warehouse: str | None,
    model_version: int | None,
    explain: bool,
    dry_run: bool,
    project: str | None,
    **values: Any,  # noqa: ANN401
) -> None:
    from databay.cli.output import emit_grid  # noqa: PLC0415
    from databay.invoke import CallValues  # noqa: PLC0415
    from databay.operations.metrics import METRICS_QUERY  # noqa: PLC0415

    filters = [parse_where(text) for text in where]
    time = {k: v for k, v in {"grain": grain, "last": last, "from": start, "to": end}.items() if v is not None}
    body: dict[str, Any] = {"metric": metric, "explain": explain, "dry_run": dry_run}
    optional = {
        "dimensions": list(by) or None,
        "time": time or None,
        "filters": filters or None,
        "limit": limit,
        "warehouse": warehouse,
        "model_version": model_version,
    }
    body.update({k: v for k, v in optional.items() if v is not None})
    call = CallValues(body=body, has_body=True, headers={CALLER_HEADER: "cli"}, project_flag=project)
    console, result = _call(ctx, values, METRICS_QUERY, call)
    answer = result.parsed
    if answer.truncated:
        console.notice(
            "truncated",
            f"the server returned the first {answer.row_count} rows",
            only_on_terminal=console.output == "json",
        )
    emit_grid(console, _envelope(result), [c.name for c in answer.columns], [list(row) for row in answer.rows])
    if answer.explain is not None and console.output == "table":
        console.stdout.write(f"sql:\n{answer.explain.sql.text}\n")
