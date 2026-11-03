"""``databay pipe wait``: when a pipe is active, and when its table is ready to query."""

from __future__ import annotations

import json

import httpx
import pytest
from cli_parse import parse
from conftest import SERVER, TOKEN, answer

from databay.cli.pipe_wait import DATA_WAIT, wait_for_data
from databay.errors import DatabayError, DatabayWaitError
from databay.invoke import Session
from databay.models.shared import FreshnessState, PipeHealth
from databay.operations.pipe import PIPE_STATUS
from databay.tokens import StaticToken
from databay.transport import Transport

PIPE = "11111111-2222-3333-4444-555555555555"
SINK = "22222222-3333-4444-5555-666666666666"
STATUS = f"/api/cdc/pipe/{PIPE}/status"
DETAIL = f"/api/cdc/pipe/{PIPE}"
DESTINATION = f"/api/cdc/pipe/{PIPE}/destination"
WAREHOUSE = f"/api/configstore/databases/{SINK}"
QUERY = f"databay sql --db {SINK} 'select count(*) from analytics.orders'"  # noqa: S608 — a command line


def _status(value: str = "active", **extra: object) -> httpx.Response:
    return answer(200, {"pipe_uuid": PIPE, "pipe_status": value, "health": "healthy", **extra})


def _pipe(freshness: str | None = "behind", health: str | None = "healthy", **extra: object) -> httpx.Response:
    return answer(
        200,
        {
            "uuid": PIPE,
            "source_id": "s1",
            "sink_db_uuid": SINK,
            "dest_table_name": "orders",
            "project_id": "proj-active",
            "status": "active",
            "freshness_state": freshness,
            "health": health,
            **extra,
        },
    )


def _destination(**fields: object) -> httpx.Response:
    return answer(
        200,
        {
            "pipe_uuid": PIPE,
            "as_of": "2026-10-31T00:00:00Z",
            "volume_available": True,
            "freshness_available": True,
            "destination_table_exists": True,
            "stored_rows": 0,
            **fields,
        },
    )


def _unavailable(code: str, *, retriable: bool | None, **fields: object) -> httpx.Response:
    return _destination(
        stats_unavailable=True,
        unavailable_code=code,
        retriable=retriable,
        destination_table_exists=None,
        stored_rows=None,
        **fields,
    )


def _warehouse(db_name: str | None = "analytics") -> httpx.Response:
    return answer(
        200,
        {
            "uuid": SINK,
            "name": "w",
            "host": "h",
            "port": 1,
            "type": "clickhouse",
            "username": "u",
            "db_name": db_name,
            "access_capabilities": {"column_grants": False, "row_filters": False, "logins": False},
        },
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _wait(server, timeout: float | None = None, clock: Clock | None = None):
    clock = clock or Clock()
    transport = Transport(server=SERVER, tokens=StaticToken(TOKEN), client_kind="sdk", http=server.client())
    return wait_for_data(Session(transport), PIPE, timeout_s=timeout, clock=clock, sleep=clock.sleep)


def _active(server, *pipe: httpx.Response) -> None:
    server.on("GET", STATUS, _status())
    server.on("GET", DETAIL, *pipe)


def test_every_generated_freshness_state_is_classified_once():
    named = [*DATA_WAIT.pending, *DATA_WAIT.success, *DATA_WAIT.failure, *[v for v, _ in DATA_WAIT.conditional]]

    assert sorted(named) == sorted(state.value for state in FreshnessState)


def test_every_generated_pipe_health_is_classified_once():
    [(_, rule)] = DATA_WAIT.conditional

    assert sorted((*rule.pending, *rule.success, *rule.failure)) == sorted(health.value for health in PipeHealth)


def test_until_active_runs_the_wait_pipe_status_runs(cli, server):
    server.on("GET", STATUS, _status("active"))

    run = cli("pipe", "wait", PIPE, "--until", "active")

    assert run.code == 0, run.stderr
    body = run.json()
    assert body["result"]["pipe_status"] == "active"
    assert body["wait"]["status_operation"] == PIPE_STATUS.operation_id
    assert body["next"] == f"databay pipe wait {PIPE} --until data"
    assert server.calls("GET", DESTINATION) == []


def test_the_next_after_until_active_keeps_the_callers_timeout(cli, server):
    server.on("GET", STATUS, _status("active"))

    run = cli("pipe", "wait", PIPE, "--until", "active", "--timeout", "540")

    assert run.json()["next"] == f"databay pipe wait {PIPE} --until data --timeout 540"


def test_until_active_stops_on_the_status_the_declared_wait_calls_a_failure(cli, server):
    server.on("GET", STATUS, _status("failed", last_error="bad credentials"))

    run = cli("pipe", "wait", PIPE, "--until", "active")

    assert run.code == 5
    assert run.json()["result"]["pipe_status"] == "failed"
    assert "bad credentials" in run.error()["message"]


def test_until_active_that_times_out_resumes_with_the_same_command(cli, server):
    server.on("GET", STATUS, _status("creating"))

    run = cli("pipe", "wait", PIPE, "--until", "active", "--timeout", "0.001")

    assert run.code == 4
    assert run.json()["next"] == f"databay pipe wait {PIPE} --until active --timeout 0.001"


def test_until_is_required(cli):
    run = cli("pipe", "wait", PIPE)

    assert run.code == 2
    assert "--until" in run.error()["message"]


def test_data_waits_for_active_without_measuring_the_table(server):
    server.on("GET", STATUS, _status("pending"), _status("creating"), _status("active"))
    server.on("GET", DETAIL, _pipe())
    server.on("GET", DESTINATION, _destination(stored_rows=5))
    clock = Clock()

    outcome = _wait(server, clock=clock)

    assert outcome.reason == "rows_landed"
    assert clock.sleeps == [2.0, 3.0]
    assert len(server.calls("GET", DESTINATION)) == 1
    assert len(server.calls("GET", STATUS)) == 3


def test_a_paused_quota_pipe_reports_its_status_not_a_freshness_failure(cli, server):
    server.on("GET", STATUS, _status("paused_quota"))

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 5
    assert run.json()["result"]["pipe_status"] == "paused_quota"
    assert run.error()["details"]["state"] == "paused_quota"
    assert server.calls("GET", DESTINATION) == []


def test_a_pipe_that_leaves_active_while_the_table_is_empty_reports_its_status(server):
    _active(server, _pipe(), _pipe(status="paused_quota", last_error="quota reached"))
    server.on("GET", DESTINATION, _destination())

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server)

    assert caught.value.outcome.status["pipe_status"] == "paused_quota"
    assert "quota reached" in caught.value.message
    assert len(server.calls("GET", DESTINATION)) == 1


def test_a_pipe_rebuilding_after_active_stays_pending_without_measuring(server):
    _active(server, _pipe(status="creating"), _pipe())
    server.on("GET", DESTINATION, _destination(stored_rows=1))

    outcome = _wait(server)

    assert outcome.reason == "rows_landed"
    assert len(server.calls("GET", DESTINATION)) == 1


def test_a_status_this_client_does_not_know_stops_the_data_wait(server):
    _active(server, _pipe(status="migrating"))

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server)

    assert caught.value.code == "unexpected_wait_state"
    assert caught.value.exit_code == 5


def test_rows_in_the_table_end_the_wait_while_freshness_is_still_behind(cli, server):
    _active(server, _pipe("behind"))
    server.on("GET", DESTINATION, _destination(stored_rows=42))
    server.on("GET", WAREHOUSE, _warehouse())

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 0, run.stderr
    body = run.json()
    assert body["wait"]["outcome"] == "success"
    assert body["wait"]["reason"] == "rows_landed"
    assert body["result"]["stored_rows"] == 42
    assert body["next"] == QUERY


def test_rows_end_the_wait_even_when_freshness_says_stale(server):
    _active(server, _pipe("stale"))
    server.on("GET", DESTINATION, _destination(stored_rows=1))

    assert _wait(server).reason == "rows_landed"


NOT_SUPPORTED = _unavailable("not_supported", retriable=False)


@pytest.mark.parametrize(
    ("freshness", "health"),
    [("fresh", "healthy"), ("fresh", "not_reporting"), ("unknown", "healthy")],
)
def test_a_table_that_cannot_be_measured_ends_on_the_healthy_verdict(cli, server, freshness, health):
    _active(server, _pipe(freshness, health))
    server.on("GET", DESTINATION, NOT_SUPPORTED)
    server.on("GET", WAREHOUSE, _warehouse())

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 0, run.stderr
    assert run.json()["wait"]["reason"] == "healthy_unmeasured"
    assert run.json()["next"] == QUERY


def test_a_table_that_cannot_be_measured_fails_on_stale(cli, server):
    _active(server, _pipe("stale", health_detail="no change for 3 h"))
    server.on("GET", DESTINATION, NOT_SUPPORTED)

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 5
    assert run.error()["code"] == "wait_failed"
    assert "no change for 3 h" in run.error()["message"]


def test_a_table_that_cannot_be_measured_fails_on_not_landing_data(cli, server):
    _active(server, _pipe("fresh", "not_landing_data", health_detail="connector crashed"))
    server.on("GET", DESTINATION, NOT_SUPPORTED)

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 5
    assert "connector crashed" in run.error()["message"]


@pytest.mark.parametrize(
    ("pipe", "state"),
    [
        (_pipe("never_checked"), "never_checked"),
        (_pipe("behind"), "behind"),
        (_pipe(None), "freshness_state=null health=healthy"),
        (_pipe("fresh", None), "freshness_state=fresh health=null"),
        (_pipe("unknown", "unknown"), "unknown (health=unknown)"),
        (_pipe("unknown", "stale"), "unknown (health=stale)"),
        (_pipe("unknown", "not_reporting"), "unknown (health=not_reporting)"),
        (_pipe("unknown", "not_monitored"), "unknown (health=not_monitored)"),
    ],
)
def test_a_table_that_cannot_be_measured_stays_pending_without_a_verdict(server, pipe, state):
    _active(server, pipe)
    server.on("GET", DESTINATION, NOT_SUPPORTED)
    clock = Clock()

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server, timeout=10, clock=clock)

    assert caught.value.code == "wait_pending"
    assert caught.value.outcome.state == state
    assert sum(clock.sleeps) == pytest.approx(10)


@pytest.mark.parametrize(
    ("freshness", "health", "state"),
    [
        ("fresh", "healthy", "healthy but no rows have landed"),
        ("unknown", "healthy", "healthy but no rows have landed"),
        ("behind", "healthy", "healthy but no rows have landed"),
        ("never_checked", "healthy", "healthy but no rows have landed"),
        (None, "healthy", "healthy but no rows have landed"),
        ("fresh", "not_reporting", "no rows have landed (health=not_reporting)"),
        ("unknown", "stale", "no rows have landed (health=stale)"),
        ("fresh", None, "no rows have landed (health=null)"),
    ],
)
def test_a_measured_empty_table_stays_pending_in_every_freshness_state(server, freshness, health, state):
    _active(server, _pipe(freshness, health))
    server.on("GET", DESTINATION, _destination(stored_rows=0))
    clock = Clock()

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server, timeout=10, clock=clock)

    assert caught.value.code == "wait_pending"
    assert caught.value.outcome.state == state
    assert caught.value.outcome.reason is None
    assert sum(clock.sleeps) == pytest.approx(10)


def test_an_empty_table_times_out_saying_the_pipe_is_healthy_and_can_resume(cli, server):
    _active(server, _pipe("fresh"))
    server.on("GET", DESTINATION, _destination(stored_rows=0))

    run = cli("pipe", "wait", PIPE, "--until", "data", "--timeout", "0.001")

    assert run.code == 4
    assert "still healthy but no rows have landed after" in run.error()["message"]
    assert run.json()["next"] == f"databay pipe wait {PIPE} --until data --timeout 0.001"
    assert server.calls("GET", WAREHOUSE) == []


def test_an_empty_table_says_healthy_only_when_health_is(cli, server):
    _active(server, _pipe("fresh", "not_reporting"))
    server.on("GET", DESTINATION, _destination(stored_rows=0))

    run = cli("pipe", "wait", PIPE, "--until", "data", "--timeout", "0.001")

    assert run.code == 4
    assert "no rows have landed" in run.error()["message"]
    assert "healthy" not in run.error()["message"]


@pytest.mark.parametrize(
    ("freshness", "health"),
    [("fresh", "healthy"), ("unknown", "healthy"), ("behind", "healthy")],
)
@pytest.mark.parametrize(
    ("fields", "state"),
    [
        ({"destination_table_exists": True, "stored_rows": None}, "healthy but the row count was not measured"),
        ({"destination_table_exists": None, "stored_rows": None}, "healthy but the row count was not measured"),
        (
            {"destination_table_exists": False, "stored_rows": None},
            "healthy but the destination table does not exist yet",
        ),
        ({"destination_table_exists": False, "stored_rows": 9}, "healthy but the destination table does not exist yet"),
    ],
    ids=["no-count", "not-checked", "no-table", "no-table-with-a-count"],
)
def test_a_null_or_missing_measure_that_is_not_not_supported_never_ends_the_wait(
    server, freshness, health, fields, state
):
    _active(server, _pipe(freshness, health))
    server.on("GET", DESTINATION, _destination(**fields))

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server, timeout=5)

    assert caught.value.code == "wait_pending"
    assert caught.value.outcome.state == state


@pytest.mark.parametrize("freshness", ["stale", "not_monitored"])
def test_a_freshness_failure_exits_5_with_the_health_detail(cli, server, freshness):
    _active(server, _pipe(freshness, health_detail="no change for 3 h"))
    server.on("GET", DESTINATION, _destination())

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 5
    assert run.error()["code"] == "wait_failed"
    assert "no change for 3 h" in run.error()["message"]
    assert run.json()["wait"]["state"] == freshness


@pytest.mark.parametrize("freshness", ["behind", "fresh", "never_checked", "unknown", None])
def test_not_landing_data_fails_in_any_freshness_state(cli, server, freshness):
    _active(server, _pipe(freshness, "not_landing_data", health_detail="connector crashed"))

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 5
    assert run.error()["code"] == "wait_failed"
    assert "connector crashed" in run.error()["message"]
    assert run.json()["result"]["health_detail"] == "connector crashed"
    assert "next" not in run.json()


def test_not_landing_data_fails_even_when_the_table_holds_rows(server):
    _active(server, _pipe("behind", "not_landing_data"))
    server.on("GET", DESTINATION, _destination(stored_rows=7))

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server)

    assert caught.value.details["state"] == "not_landing_data"


@pytest.mark.parametrize(
    "pipe",
    [_pipe("sideways"), _pipe("behind", "wobbling"), _pipe("fresh", "wobbling"), _pipe("unknown", "wobbling")],
    ids=["freshness", "health-behind", "health-fresh", "health-unknown"],
)
def test_a_value_the_rules_do_not_name_stops_with_an_upgrade_hint(cli, server, pipe):
    _active(server, pipe)
    server.on("GET", DESTINATION, _destination())

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 5
    assert run.error()["code"] == "unexpected_wait_state"
    assert "Upgrade the CLI" in run.error()["hint"]


def test_not_provisioned_stays_pending_then_ends_on_rows(server):
    _active(server, _pipe())
    server.on(
        "GET",
        DESTINATION,
        _unavailable("not_provisioned", retriable=True),
        _destination(stored_rows=3),
    )

    assert _wait(server).reason == "rows_landed"
    assert len(server.calls("GET", DESTINATION)) == 2


def test_a_retriable_unavailable_code_is_retried_past_the_transient_limit(server):
    _active(server, _pipe())
    down = _unavailable("warehouse_unreachable", retriable=True)
    server.on("GET", DESTINATION, down, down, down, down, _destination(stored_rows=3))

    assert _wait(server).reason == "rows_landed"
    assert len(server.calls("GET", DESTINATION)) == 5


def test_a_retriable_unavailable_code_times_out_with_its_state(server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, _unavailable("warehouse_unreachable", retriable=True))

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server, timeout=5)

    assert caught.value.code == "wait_pending"
    assert caught.value.outcome.state == "stats_unavailable (warehouse_unreachable)"


def test_a_non_retriable_unavailable_code_stops_with_the_server_remediation_as_hint(cli, server):
    _active(server, _pipe())
    server.on(
        "GET",
        DESTINATION,
        _unavailable(
            "invalid_identifier",
            retriable=False,
            unavailable_message="This pipe's destination table name cannot be queried safely.",
            remediation="Re-create this pipe with a destination table name of letters.",
            support_ref="ref-1",
        ),
    )

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 5
    error = run.error()
    assert error["code"] == "wait_failed"
    assert error["hint"] == "Re-create this pipe with a destination table name of letters."
    assert error["support_ref"] == "ref-1"
    assert error["details"]["unavailable_code"] == "invalid_identifier"
    assert len(server.calls("GET", DESTINATION)) == 1


def test_a_new_unavailable_code_follows_its_retriable_flag(server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, _unavailable("warehouse_rebooting", retriable=False))

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server)

    assert caught.value.code == "wait_failed"


def test_an_unavailable_answer_that_does_not_say_if_a_retry_helps_stops(server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, _unavailable("internal", retriable=None))

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server)

    assert caught.value.code == "unexpected_wait_state"


def test_the_data_wait_defaults_to_its_declared_timeout(server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, _destination())
    clock = Clock()

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server, clock=clock)

    assert caught.value.details["timeout_s"] == 1200.0
    assert sum(clock.sleeps) == pytest.approx(1200.0)


def test_a_timed_out_data_wait_exits_4_and_its_next_resumes_it(cli, server):
    server.on("GET", STATUS, _status("creating"))

    first = cli("pipe", "wait", PIPE, "--until", "data", "--timeout", "0.001")

    assert first.code == 4
    assert first.error()["code"] == "wait_pending"
    assert first.json()["wait"]["state"] == "creating"
    resume = first.json()["next"]
    assert resume == f"databay pipe wait {PIPE} --until data --timeout 0.001"
    assert first.error()["next"] == resume

    server.on("GET", STATUS, _status("active"))
    server.on("GET", DETAIL, _pipe())
    server.on("GET", DESTINATION, _destination(stored_rows=2))
    server.on("GET", WAREHOUSE, _warehouse())

    second = cli(*resume.split()[1:])

    assert second.code == 0, second.stderr
    assert second.json()["wait"]["reason"] == "rows_landed"
    assert parse(second.json()["next"]).command.name == "sql"


def test_the_resume_keeps_the_profile(cli, server, profile_env):
    config = profile_env["config"] / "config.toml"
    config.write_text(config.read_text() + '\n[profiles.other]\nserver = "http://databay.test"\nauth = "env"\n')
    server.on("GET", STATUS, _status("creating"))

    run = cli("pipe", "wait", PIPE, "--until", "data", "--timeout", "0.001", "--profile", "other")

    assert run.json()["next"] == f"databay pipe wait {PIPE} --until data --timeout 0.001 --profile other"


def test_each_state_change_is_one_event_on_stderr(cli, server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, _destination(), _destination(stored_rows=1))
    server.on("GET", WAREHOUSE, _warehouse())

    run = cli("pipe", "wait", PIPE, "--until", "data", "--timeout", "1")

    assert run.code == 0, run.stderr
    assert [event["state"] for event in run.events("wait")] == ["healthy but no rows have landed", "stored_rows=1"]


def test_the_sql_next_names_the_pipes_project_when_it_is_not_the_active_one(cli, server):
    _active(server, _pipe(project_id="proj-other"))
    server.on("GET", DESTINATION, _destination(stored_rows=1))
    server.on("GET", WAREHOUSE, _warehouse())

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 0, run.stderr
    assert server.calls("GET", WAREHOUSE)[0].headers["x-databay-project-id"] == "proj-other"
    assert run.json()["next"] == QUERY.replace(f"{SINK} ", f"{SINK} --project proj-other ")


@pytest.mark.parametrize(
    ("warehouse", "code"),
    [
        (answer(403, {"detail": "no", "code": "permission_denied"}), "permission_denied"),
        (_warehouse(db_name=None), "db_name_missing"),
    ],
    ids=["refused", "no-name"],
)
def test_a_ready_table_whose_warehouse_cannot_be_read_still_succeeds(cli, server, warehouse, code):
    _active(server, _pipe())
    server.on("GET", DESTINATION, _destination(stored_rows=1))
    server.on("GET", WAREHOUSE, warehouse)

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 0, run.stderr
    assert run.json()["wait"]["reason"] == "rows_landed"
    assert run.json()["next"] == f"databay warehouse get {SINK}"
    assert run.events("warehouse_unread") == [{"event": "warehouse_unread", "sink_db_uuid": SINK, "code": code}]


def test_the_warehouse_name_is_asked_for_once_after_the_wait_succeeds(cli, server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, _destination(stored_rows=1))
    server.on("GET", WAREHOUSE, answer(503, {"detail": "busy", "code": "auth_unavailable"}))

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 0, run.stderr
    assert run.json()["next"] == f"databay warehouse get {SINK}"
    assert len(server.calls("GET", WAREHOUSE)) == 1


def test_a_tick_that_runs_past_the_limit_sends_no_further_request(server):
    clock = Clock()

    def slow_pipe(_request: httpx.Request) -> httpx.Response:
        clock.now += 66
        return _pipe()

    server.on("GET", STATUS, _status())
    server.on("GET", DETAIL, slow_pipe)

    with pytest.raises(DatabayWaitError) as caught:
        _wait(server, timeout=60, clock=clock)

    assert caught.value.code == "wait_pending"
    assert caught.value.exit_code == 4
    assert server.calls("GET", DESTINATION) == []


def test_a_404_on_the_destination_route_is_wait_target_gone(cli, server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, answer(404, {"detail": "Pipe not found", "code": "pipe_not_found"}))

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 5
    assert run.error()["code"] == "wait_target_gone"
    assert run.error()["next"] is None


def test_transient_failures_on_either_route_count_in_a_row_until_a_tick_completes(server):
    busy = answer(503, {"detail": "busy", "code": "auth_unavailable"})
    server.on("GET", STATUS, _status())
    server.on("GET", DETAIL, busy, _pipe())
    server.on("GET", DESTINATION, busy, _destination(), busy, busy, busy)

    with pytest.raises(DatabayError) as caught:
        _wait(server)

    assert caught.value.code == "auth_unavailable"
    assert len(server.calls("GET", DESTINATION)) == 5


def test_retry_after_on_a_tick_sets_the_next_pause(server):
    busy = answer(503, {"detail": "busy", "code": "auth_unavailable"}, {"retry-after": "9"})
    server.on("GET", STATUS, busy, _status())
    server.on("GET", DETAIL, _pipe())
    server.on("GET", DESTINATION, _destination(stored_rows=1))
    clock = Clock()

    _wait(server, clock=clock)

    assert clock.sleeps == [9.0]


def test_a_refusal_stops_the_data_wait_at_once(cli, server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, answer(403, {"detail": "no", "code": "permission_denied"}))

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 1
    assert run.error()["code"] == "permission_denied"
    assert len(server.calls("GET", DESTINATION)) == 1


def test_an_expired_token_stops_the_wait_and_offers_the_resume(cli, server):
    server.on("GET", STATUS, answer(401, {"detail": "Could not validate credentials", "code": "token_invalid"}))

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 3
    assert run.error()["next"] == f"databay pipe wait {PIPE} --until data"


def test_a_transient_stop_offers_the_resume(cli, server):
    server.on("GET", STATUS, answer(503, {"detail": "busy", "code": "auth_unavailable"}))

    run = cli("pipe", "wait", PIPE, "--until", "data")

    assert run.code == 6
    assert run.error()["next"] == f"databay pipe wait {PIPE} --until data"
    assert run.stdout == ""


def test_table_output_names_the_reason(cli, server):
    _active(server, _pipe())
    server.on("GET", DESTINATION, _destination(stored_rows=2))
    server.on("GET", WAREHOUSE, _warehouse())

    run = cli("pipe", "wait", PIPE, "--until", "data", "--output", "table")

    assert run.code == 0, run.stderr
    assert "wait: success (rows_landed, stored_rows=2," in run.stdout
    assert run.events("next") == [{"event": "next", "value": QUERY}]


def test_json_output_carries_only_the_fields_that_decide_the_wait(cli, server):
    _active(server, _pipe("fresh", last_error=None))
    server.on("GET", DESTINATION, _destination(stored_rows=3, compressed_bytes=99))
    server.on("GET", WAREHOUSE, _warehouse())

    run = cli("pipe", "wait", PIPE, "--until", "data", "--output", "json")

    assert json.loads(run.stdout)["result"] == {
        "pipe_uuid": PIPE,
        "pipe_status": "active",
        "last_error": None,
        "freshness_state": "fresh",
        "health": "healthy",
        "health_detail": None,
        "destination_table_exists": True,
        "stored_rows": 3,
        "unavailable_code": None,
        "sink_db_uuid": SINK,
        "dest_table_name": "orders",
        "project_id": "proj-active",
    }
