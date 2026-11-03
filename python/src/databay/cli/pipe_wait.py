"""
``databay pipe wait``: when a pipe is provisioned, and when its table is ready to query.

``active`` is the wait ``pipe status --wait`` runs. ``data`` waits for that first, then reads the
pipe and measures its destination each tick. It ends on rows in the table. Only a destination
that reports ``not_supported`` can end on a healthy freshness verdict: there, no row count exists.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import click

from databay.cli.common import common_options, console_for
from databay.operation import Condition, Wait
from databay.settings import SETTINGS

if TYPE_CHECKING:
    from collections.abc import Callable

    from databay.cli.console import Console
    from databay.invoke import Session
    from databay.waits import Fetch, Reading, WaitOutcome

COMMAND = "databay pipe wait"

#: A pipe's freshness once it is active. ``not_landing_data`` fails in every freshness state and a
#: null value is pending: ``data_step`` applies both, because ``classify`` reads only one value.
#: Success ends the wait only for a table that cannot be measured; see ``_no_rows_yet``.
DATA_WAIT = Wait(
    status_operation="pipe_get",
    bind=(("pipe_uuid", "path:pipe_uuid"),),
    field="/freshness_state",
    pending=("never_checked", "behind"),
    success=("fresh",),
    failure=("stale", "not_monitored"),
    error_field="/health_detail",
    conditional=(
        (
            "unknown",
            Condition(
                field="/health",
                pending=("unknown", "stale", "not_reporting", "not_monitored"),
                success=("healthy",),
                failure=("not_landing_data",),
            ),
        ),
    ),
)

PIPE_WAIT_HELP = """Wait until a pipe is active, or until its table is ready to query.

\b
--until active ends when the pipe is provisioned; data may not have arrived.
--until data waits for active, then ends when the table holds rows
(rows_landed). A warehouse that cannot report a row count ends when the
pipe reports healthy (healthy_unmeasured). An empty table keeps waiting.
\b
A pipe that ends in another status reports that status. A pipe that is not
landing data, or whose stats fail for good, exits 5. Running out of time
exits 4, and `next` runs the same wait again.

\b
Examples:
  databay pipe wait <pipe_uuid> --until active
  databay pipe wait <pipe_uuid> --until data --timeout 540"""


def _summary(
    pipe_uuid: str, pipe_status: str, answer: dict[str, Any], destination: dict[str, Any] | None
) -> dict[str, Any]:
    """What the wait prints: the fields of the two answers that decide it, and what ``next`` is built from."""
    measured = destination or {}
    return {
        "pipe_uuid": pipe_uuid,
        "pipe_status": pipe_status,
        "last_error": answer.get("last_error"),
        "freshness_state": answer.get("freshness_state"),
        "health": answer.get("health"),
        "health_detail": answer.get("health_detail"),
        "destination_table_exists": measured.get("destination_table_exists"),
        "stored_rows": measured.get("stored_rows"),
        "unavailable_code": measured.get("unavailable_code"),
        "sink_db_uuid": answer.get("sink_db_uuid"),
        "dest_table_name": answer.get("dest_table_name"),
        "project_id": answer.get("project_id"),
    }


def _unavailable(destination: dict[str, Any], status: dict[str, Any]) -> Reading:
    """A destination that could not be measured: retried when the server says a retry may help."""
    from databay.errors import DatabayWaitError  # noqa: PLC0415
    from databay.waits import Reading  # noqa: PLC0415

    code = destination.get("unavailable_code")
    state = f"stats_unavailable ({code})"
    retriable = destination.get("retriable")
    if retriable is True:
        return Reading("pending", state, status, "pipe_destination_get")
    if retriable is None:
        stop = DatabayWaitError(
            f"{COMMAND}: the destination reports stats_unavailable ({code}) without saying if a retry may help",
            code="unexpected_wait_state",
            details={"state": state},
        )
        return Reading("failure", state, status, "pipe_destination_get", stop)
    message = destination.get("unavailable_message")
    stop = DatabayWaitError(
        f"{COMMAND}: the destination cannot be measured ({code})" + (f": {message}" if message else ""),
        hint=destination.get("remediation"),
        support_ref=destination.get("support_ref"),
        details={"state": state, "unavailable_code": code, "error": message},
    )
    return Reading("failure", state, status, "pipe_destination_get", stop)


def _freshness(pipe: dict[str, Any], status: dict[str, Any]) -> Reading:
    from databay.errors import DatabayWaitError  # noqa: PLC0415
    from databay.invoke import json_text  # noqa: PLC0415
    from databay.waits import Reading, read_status  # noqa: PLC0415

    freshness, health = pipe.get("freshness_state"), pipe.get("health")
    if freshness is None or health is None:
        state = f"freshness_state={json_text(freshness)} health={json_text(health)}"
        return Reading("pending", state, status, DATA_WAIT.status_operation)
    health_rule = DATA_WAIT.conditional[0][1]
    if health not in (*health_rule.pending, *health_rule.success, *health_rule.failure):
        stop = DatabayWaitError(
            f"{COMMAND}: health is {health!r}, a state this CLI does not know",
            code="unexpected_wait_state",
            details={"state": health},
        )
        return Reading("failure", health, status, DATA_WAIT.status_operation, stop)
    reading = read_status(DATA_WAIT, pipe, COMMAND)
    reason = "healthy_unmeasured" if reading.outcome == "success" else None
    return replace(reading, status=status, reason=reason)


def _no_rows_yet(pipe: dict[str, Any], destination: dict[str, Any], status: dict[str, Any]) -> Reading:
    """A destination that can be measured and shows no rows: only a failure verdict ends the wait."""
    from databay.invoke import json_text  # noqa: PLC0415
    from databay.waits import Reading  # noqa: PLC0415

    verdict = _freshness(pipe, status)
    if verdict.outcome == "failure":
        return verdict
    exists = destination.get("destination_table_exists")
    if exists is False:
        missing = "the destination table does not exist yet"
    elif exists and destination.get("stored_rows") == 0:
        missing = "no rows have landed"
    else:
        missing = "the row count was not measured"
    health = pipe.get("health")
    state = f"healthy but {missing}" if health == "healthy" else f"{missing} (health={json_text(health)})"
    return Reading("pending", state, status, "pipe_destination_get")


def _measured(pipe: dict[str, Any], destination: dict[str, Any], status: dict[str, Any]) -> Reading:
    """What an active pipe's destination answer means."""
    from databay.waits import Reading  # noqa: PLC0415

    if destination.get("stats_unavailable"):
        if destination.get("unavailable_code") == "not_supported":
            return _freshness(pipe, status)
        return _unavailable(destination, status)
    stored = destination.get("stored_rows")
    if destination.get("destination_table_exists") and stored is not None and stored > 0:
        return Reading("success", f"stored_rows={stored}", status, "pipe_destination_get", reason="rows_landed")
    return _no_rows_yet(pipe, destination, status)


def data_step(pipe_uuid: str) -> Callable[[Fetch], Reading]:
    """One tick of ``--until data``: the status route until the pipe is active, then its two routes."""
    from databay.errors import DatabayWaitError  # noqa: PLC0415
    from databay.invoke import CallValues, build_request  # noqa: PLC0415
    from databay.operations.pipe import PIPE_GET, PIPE_STATUS  # noqa: PLC0415
    from databay.operations.pipe_destination import PIPE_DESTINATION_GET  # noqa: PLC0415
    from databay.waits import Reading, read_status  # noqa: PLC0415

    active_wait = PIPE_STATUS.resumes
    assert active_wait is not None
    pipe_status_wait = replace(active_wait, status_operation=PIPE_GET.operation_id, field="/status")
    values = CallValues(path={"pipe_uuid": pipe_uuid})
    status_request = build_request(PIPE_STATUS, values, None)
    pipe_request = build_request(PIPE_GET, values, None)
    destination_request = build_request(PIPE_DESTINATION_GET, values, None)
    has_been_active = False

    def step(fetch: Fetch) -> Reading:
        nonlocal has_been_active
        if not has_been_active:
            answer = fetch(PIPE_STATUS, status_request)
            reading = read_status(active_wait, answer, COMMAND)
            if reading.outcome != "success":
                return replace(reading, status=_summary(pipe_uuid, reading.state, answer, None))
            has_been_active = True
        pipe = fetch(PIPE_GET, pipe_request)
        reading = read_status(pipe_status_wait, pipe, COMMAND)
        if reading.outcome != "success":
            return replace(reading, status=_summary(pipe_uuid, reading.state, pipe, None))
        if pipe.get("health") == "not_landing_data":
            status = _summary(pipe_uuid, reading.state, pipe, None)
            detail = pipe.get("health_detail")
            stop = DatabayWaitError(
                f"{COMMAND}: the pipe is not landing data" + (f": {detail}" if detail else ""),
                details={"state": "not_landing_data", "error": detail},
            )
            return Reading("failure", "not_landing_data", status, PIPE_GET.operation_id, stop)
        destination = fetch(PIPE_DESTINATION_GET, destination_request)
        return _measured(pipe, destination, _summary(pipe_uuid, reading.state, pipe, destination))

    return step


def wait_for_data(
    session: Session,
    pipe_uuid: str,
    *,
    timeout_s: float | None,
    on_state: Callable[[WaitOutcome], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WaitOutcome:
    from databay import waits  # noqa: PLC0415

    return waits.poll(
        session,
        data_step(pipe_uuid),
        command=COMMAND,
        status_operation="pipe_status",
        bound={"pipe_uuid": pipe_uuid},
        limit=timeout_s if timeout_s is not None else session.transport.settings.wait.data_timeout_s,
        on_state=on_state,
        clock=clock,
        sleep=sleep,
    )


def _profile_words(profile: str | None) -> list[str]:
    return ["--profile", profile] if profile is not None else []


def _resume(pipe_uuid: str, until: str, timeout: float | None, profile: str | None) -> str:
    words = ["databay", "pipe", "wait", pipe_uuid, "--until", until]
    if timeout is not None:
        words += ["--timeout", f"{timeout:g}"]
    return shlex.join(words + _profile_words(profile))


def _query_next(session: Session, console: Console, status: dict[str, Any], profile: str | None) -> str:
    """
    The ``sql`` command counting the table's rows, on the pipe's own warehouse.

    The table is already ready, so a warehouse that cannot be read changes only ``next``: it becomes
    the command that reads the name, and a ``warehouse_unread`` event says why. One attempt, because
    the answer is not printed until this returns and ``--timeout`` may be spent.
    """
    from databay.errors import DatabayError  # noqa: PLC0415
    from databay.invoke import CallValues, build_request, decode, resolve_project  # noqa: PLC0415
    from databay.operations.warehouse import WAREHOUSE_GET  # noqa: PLC0415
    from databay.transport import error_from  # noqa: PLC0415

    sink = status["sink_db_uuid"]
    project = status["project_id"]
    flags = ["--project", project] if project is not None and project != session.active_project else []
    flags += _profile_words(profile)
    read_name = shlex.join(["databay", "warehouse", "get", sink, *flags])
    call = CallValues(path={"db_uuid": sink}, project_explicit=project)
    try:
        request = build_request(WAREHOUSE_GET, call, resolve_project(WAREHOUSE_GET, call, session.active_project))
        answer = session.transport.exchange(request, retries=False)
        if answer.status >= 400:  # noqa: PLR2004
            raise error_from(answer, request)
        database = decode(WAREHOUSE_GET, answer)[0].get("db_name")
    except DatabayError as exc:
        console.event("warehouse_unread", {"sink_db_uuid": sink, "code": exc.code})
        return read_name
    if database is None:
        console.event("warehouse_unread", {"sink_db_uuid": sink, "code": "db_name_missing"})
        return read_name
    statement = f"select count(*) from {database}.{status['dest_table_name']}"  # noqa: S608 — printed for the caller to run; `sql` runs it read-only
    return shlex.join(["databay", "sql", "--db", sink, *flags, statement])


@click.command("wait", help=PIPE_WAIT_HELP, short_help="Wait until a pipe is active or its table is ready to query")
@click.argument("pipe_uuid", metavar="PIPE_UUID")
@click.option(
    "--until",
    "until",
    required=True,
    type=click.Choice(("active", "data")),
    help="active: provisioned; data: the table is ready to query",
)
@click.option(
    "--timeout",
    "timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    metavar="S",
    help=(
        "seconds to wait before exit 4, resumable "
        f"(default: {SETTINGS.wait.default_timeout_s:g} for active, {SETTINGS.wait.data_timeout_s:g} for data)"
    ),
)
@common_options
@click.pass_context
def pipe_wait(
    ctx: click.Context,
    pipe_uuid: str,
    until: str,
    timeout: float | None,
    **values: Any,  # noqa: ANN401
) -> None:
    from databay import waits  # noqa: PLC0415
    from databay.cli.runner import open_session, report_wait  # noqa: PLC0415
    from databay.operations.pipe import PIPE_GET, PIPE_STATUS  # noqa: PLC0415
    from databay.operations.pipe_destination import PIPE_DESTINATION_GET  # noqa: PLC0415

    console, state = console_for(ctx, values)
    session, profile = open_session(values, console, state)
    state["context"] = {"server": profile.server, "profile": profile.name, "request_id": session.transport.request_id}
    state["reading"] = True
    explicit_profile = values.get("profile")
    resume = _resume(pipe_uuid, until, timeout, explicit_profile)
    active_wait = PIPE_STATUS.resumes
    assert active_wait is not None

    def run_active(on_state: Callable[[WaitOutcome], None]) -> WaitOutcome:
        return waits.run(session, active_wait, {"pipe_uuid": pipe_uuid}, timeout_s=timeout, on_state=on_state)

    def run_data(on_state: Callable[[WaitOutcome], None]) -> WaitOutcome:
        return wait_for_data(session, pipe_uuid, timeout_s=timeout, on_state=on_state)

    def active_next(_: WaitOutcome) -> str:
        return _resume(pipe_uuid, "data", timeout, explicit_profile)

    def data_next(outcome: WaitOutcome) -> str:
        return _query_next(session, console, outcome.status, explicit_profile)

    try:
        if until == "active":
            session.ensure_compatible(PIPE_STATUS)
            columns, rows = PIPE_STATUS.columns, PIPE_STATUS.response.rows
            report_wait(
                run_active, console, {"result": None}, columns, rows, resume, state, from_status=True, then=active_next
            )
            return
        for operation in (PIPE_STATUS, PIPE_GET, PIPE_DESTINATION_GET):
            session.ensure_compatible(operation)
        report_wait(run_data, console, {"result": None}, (), None, resume, state, from_status=True, then=data_next)
    finally:
        session.transport.close()
