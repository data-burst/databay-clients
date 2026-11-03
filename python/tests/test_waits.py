"""The wait and verdict state machines, on the declared pipe-create wait and the precheck verdict."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from conftest import SERVER, TOKEN, answer

from databay import waits
from databay.cli.build import operation_command
from databay.cli.flags import command_flags
from databay.errors import DatabayError, DatabayWaitError
from databay.feature_header import FEATURE_SOURCE_HINTS
from databay.invoke import Session
from databay.operations.backfill_run import BACKFILL_RUN_CREATE
from databay.operations.pipe import PIPE_CREATE, PIPE_RETRY
from databay.resource import Resource
from databay.tokens import StaticToken
from databay.transport import Transport

PIPE = "11111111-2222-3333-4444-555555555555"
STATUS = f"/api/cdc/pipe/{PIPE}/status"
RETRY = f"/api/cdc/pipe/{PIPE}/retry"


def _status(value: str, **extra: object) -> httpx.Response:
    return answer(200, {"pipe_uuid": PIPE, "pipe_status": value, "health": "healthy", **extra})


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _run(server, timeout: float | None = None, clock: Clock | None = None) -> waits.WaitOutcome:
    clock = clock or Clock()
    transport = Transport(server=SERVER, tokens=StaticToken(TOKEN), client_kind="sdk", http=server.client())
    wait = PIPE_CREATE.wait
    assert wait is not None
    return waits.run(Session(transport), wait, {"pipe_uuid": PIPE}, timeout_s=timeout, clock=clock, sleep=clock.sleep)


def test_pending_then_active_succeeds_with_growing_intervals(server):
    server.on("GET", STATUS, _status("pending"), _status("creating"), _status("creating"), _status("active"))
    clock = Clock()

    outcome = _run(server, clock=clock)

    assert outcome.outcome == "success"
    assert outcome.state == "active"
    assert clock.sleeps == [2.0, 3.0, 4.5]


def test_a_failure_state_stops_with_its_error_text(server):
    server.on("GET", STATUS, _status("creating"), _status("failed", last_error="slot exhausted"))

    with pytest.raises(DatabayWaitError) as caught:
        _run(server)

    assert caught.value.code == "wait_failed"
    assert caught.value.exit_code == 5
    assert "slot exhausted" in caught.value.message
    assert caught.value.outcome.state == "failed"


def test_a_state_this_client_does_not_know_stops_the_wait(server):
    server.on("GET", STATUS, _status("migrating"))

    with pytest.raises(DatabayWaitError) as caught:
        _run(server)

    assert caught.value.code == "unexpected_wait_state"
    assert caught.value.exit_code == 5
    assert caught.value.details["state"] == "migrating"


def test_running_out_of_time_is_pending_and_resumable(server):
    server.on("GET", STATUS, _status("creating"))
    clock = Clock()

    with pytest.raises(DatabayWaitError) as caught:
        _run(server, timeout=10, clock=clock)

    assert caught.value.code == "wait_pending"
    assert caught.value.exit_code == 4
    assert caught.value.outcome.state == "creating"
    assert sum(clock.sleeps) == pytest.approx(10)


def test_a_404_while_polling_is_wait_target_gone(server):
    server.on("GET", STATUS, _status("creating"), answer(404, {"detail": "Pipe not found", "code": "pipe_not_found"}))

    with pytest.raises(DatabayWaitError) as caught:
        _run(server)

    assert caught.value.code == "wait_target_gone"


FEATURE_OFF_HEADERS = {"X-Databay-Feature": "disabled; feature=etl; source=override"}


def test_a_feature_turned_off_while_polling_is_a_failed_wait_that_names_it(server):
    server.on("GET", STATUS, _status("creating"), answer(404, {"detail": "Not Found"}, headers=FEATURE_OFF_HEADERS))

    with pytest.raises(DatabayWaitError) as caught:
        _run(server)

    assert caught.value.code == "wait_feature_disabled"
    assert caught.value.exit_code == 5
    assert caught.value.recovery.side_effects == "unknown"
    assert caught.value.details == {"bound": {"pipe_uuid": PIPE}, "feature": "etl", "source": "override"}
    assert caught.value.hint == FEATURE_SOURCE_HINTS["override"]
    assert caught.value.outcome.state == "creating"


def test_a_status_wait_stopped_by_a_feature_keeps_the_last_status_it_read(cli, server):
    server.on("GET", STATUS, _status("creating"), answer(404, {"detail": "Not Found"}, headers=FEATURE_OFF_HEADERS))

    run = cli("pipe", "status", PIPE, "--wait")

    assert run.code == 5
    assert run.error()["code"] == "wait_feature_disabled"
    assert run.json()["result"]["pipe_status"] == "creating"
    assert run.json()["wait"]["outcome"] == "failure"


@pytest.mark.parametrize(
    "response",
    [
        answer(404, {"detail": "Pipe not found", "code": "pipe_not_found"}, headers=FEATURE_OFF_HEADERS),
        httpx.Response(
            404, text="<html>Not Found</html>", headers={"content-type": "text/html", **FEATURE_OFF_HEADERS}
        ),
        answer(404, {"detail": "Not Found"}, headers={"X-Databay-Feature": "disabled"}),
    ],
    ids=["coded", "not-json", "malformed-header"],
)
def test_a_404_a_plain_call_would_not_call_feature_off_is_still_a_target_gone(server, response):
    server.on("GET", STATUS, _status("creating"), response)

    with pytest.raises(DatabayWaitError) as caught:
        _run(server)

    assert caught.value.code == "wait_target_gone"


def test_transient_failures_are_retried_until_three_in_a_row(server):
    busy = answer(503, {"detail": "busy", "code": "auth_unavailable"})
    server.on("GET", STATUS, busy, busy, _status("creating"), busy, busy, busy)

    with pytest.raises(DatabayError) as caught:
        _run(server)

    assert caught.value.code == "auth_unavailable"
    assert len(server.calls("GET", STATUS)) == 6


def test_a_status_wait_refused_before_any_answer_prints_nothing_on_stdout(cli, server):
    server.on("GET", STATUS, answer(403, {"detail": "no", "code": "permission_denied"}))

    run = cli("pipe", "status", PIPE, "--wait")

    assert run.code == 1
    assert run.stdout == ""
    assert run.error()["next"] is None


def test_a_wait_stopped_by_an_expired_token_offers_the_resume(cli, server):
    server.on("PUT", RETRY, answer(202, RETRYING))
    server.on("GET", STATUS, answer(401, {"detail": "Could not validate credentials", "code": "token_invalid"}))

    run = cli("pipe", "retry", PIPE, "--yes", "--wait")

    assert run.code == 3
    assert run.json()["next"] == f"databay pipe status {PIPE} --wait"
    assert run.error()["next"] == run.json()["next"]


def test_a_target_gone_while_waiting_is_a_failed_outcome_without_next(cli, server):
    server.on("PUT", RETRY, answer(202, RETRYING))
    server.on("GET", STATUS, answer(404, {"detail": "Pipe not found", "code": "pipe_not_found"}))

    run = cli("pipe", "retry", PIPE, "--yes", "--wait")

    assert run.code == 5
    assert run.json()["wait"]["outcome"] == "failure"
    assert "next" not in run.json()
    assert run.error()["code"] == "wait_target_gone"


def test_another_refusal_stops_the_wait_at_once(server):
    server.on("GET", STATUS, answer(403, {"detail": "no", "code": "permission_denied"}))

    with pytest.raises(DatabayError) as caught:
        _run(server)

    assert caught.value.code == "permission_denied"
    assert len(server.calls("GET", STATUS)) == 1


RETRYING = {"status": "retrying"}


def _create_answer() -> httpx.Response:
    return answer(
        200, {"uuid": PIPE, "status": "pending", "source_id": "s1", "sink_db_uuid": "d1", "preflight_skipped": []}
    )


def test_create_without_wait_prints_the_resume_command_as_next(cli, server):
    server.on("POST", "/api/cdc/pipe", _create_answer())

    run = cli("pipe", "create", "--source-id", "s1", "--table-name", "orders", "--dest-table-name", "orders")

    assert run.code == 0, run.stderr
    body = run.json()
    assert body["result"]["uuid"] == PIPE
    assert body["project"] == "proj-active"
    assert body["next"] == f"databay pipe status {PIPE} --wait"
    assert server.calls("GET", STATUS) == []


@pytest.mark.parametrize("flag", [("--wait",), ("--timeout", "5")], ids=["wait", "timeout"])
@pytest.mark.parametrize(
    "create",
    [
        ("pipe", "create", "--source-id", "s1", "--table-name", "orders", "--dest-table-name", "orders"),
        ("backfill-run", "create", "--pipe-uuid", PIPE, "--yes"),
    ],
    ids=["pipe", "backfill-run"],
)
def test_a_create_takes_no_wait_flag_and_sends_nothing(cli, server, create, flag):
    run = cli(*create, *flag)

    assert run.code == 2
    assert flag[0] in run.error()["message"]
    assert [r for r in server.requests if r.method == "POST"] == []


def test_a_warehouse_create_answers_at_once_and_names_the_wait_as_next(cli, server):
    accepted = {
        "uuid": "w1",
        "name": "sales",
        "status": "pending",
        "status_changed_at": "2026-10-31T00:00:00Z",
        "pending_home_project_id": "proj-active",
    }
    server.on("POST", "/api/configstore/databases", answer(202, accepted))

    run = cli("warehouse", "create", "--name", "sales")

    assert run.code == 0, run.stderr
    assert run.json()["next"] == "databay warehouse get w1 --wait --project proj-active"
    assert server.calls("GET", "/api/configstore/databases/w1") == []


@pytest.mark.parametrize(
    "create",
    [
        ("db", "create", "--password", "plain"),
        ("alerting-webhook", "create", "--url", "https://hooks.example.com/token"),
    ],
    ids=["db-password", "webhook-url"],
)
def test_a_credential_is_not_a_plain_flag(cli, server, create):
    run = cli(*create)

    assert run.code == 2
    assert create[2] in run.error()["message"]
    assert [r for r in server.requests if r.method == "POST"] == []


def test_no_generated_create_offers_a_wait_and_a_retry_keeps_its_own():
    from databay.spec import OPERATIONS

    operations = [waits.operation(operation_id) for operation_id in OPERATIONS]
    creates = [op for op in operations if op.verb == "create"]

    assert [op.operation_id for op in creates if op.offers_wait] == []
    assert not [op.operation_id for op in creates if {f.role for f in command_flags(op)} & {"wait", "timeout"}]
    assert {"wait", "timeout"} <= {f.role for f in command_flags(PIPE_RETRY)}


def test_a_wait_that_times_out_exits_4_with_the_last_state_and_next(cli, server):
    server.on("PUT", RETRY, answer(202, RETRYING))
    server.on("GET", STATUS, _status("creating"))

    run = cli("pipe", "retry", PIPE, "--yes", "--wait", "--timeout", "0.001")

    assert run.code == 4
    body = run.json()
    assert body["wait"]["outcome"] == "pending"
    assert body["wait"]["state"] == "creating"
    assert body["next"] == f"databay pipe status {PIPE} --wait --timeout 0.001"
    assert run.error()["code"] == "wait_pending"
    assert run.events("wait")[0]["state"] == "creating"


def test_resuming_on_the_status_command_polls_without_creating_again(cli, server):
    server.on("GET", STATUS, _status("creating"), _status("active"))

    run = cli("pipe", "status", PIPE, "--wait", "--timeout", "5")

    assert run.code == 0, run.stderr
    body = run.json()
    assert body["wait"]["outcome"] == "success"
    assert body["result"]["pipe_status"] == "active"
    assert server.calls("POST", "/api/cdc/pipe") == []


def test_a_wait_stopped_by_transient_failures_still_prints_the_trigger_and_how_to_resume(cli, server):
    server.on("PUT", RETRY, answer(202, RETRYING))
    server.on("GET", STATUS, answer(503, {"detail": "busy", "code": "auth_unavailable"}))

    run = cli("pipe", "retry", PIPE, "--yes", "--wait", "--no-retry")

    assert run.code == 6
    body = run.json()
    assert body["result"] == RETRYING
    assert body["next"] == f"databay pipe status {PIPE} --wait"
    assert run.error()["next"] == body["next"]


BACKFILL_SKIPPED = {"pipe_uuid": PIPE, "status": "skipped", "run_uuid": None}


def test_a_create_that_started_nothing_offers_no_next(cli, server):
    server.on("POST", "/api/cdc/backfill", answer(200, BACKFILL_SKIPPED))

    run = cli("backfill-run", "create", "--pipe-uuid", PIPE, "--yes")

    assert run.code == 0, run.stderr
    body = run.json()
    assert body["result"]["status"] == "skipped"
    assert "next" not in body
    assert server.calls("GET", "/api/cdc/backfill/runs/None") == []


def test_a_wait_whose_trigger_started_nothing_reports_not_started(server, profile_env, capsys):
    server.on("POST", "/api/cdc/backfill", answer(200, BACKFILL_SKIPPED))
    command = operation_command(replace(BACKFILL_RUN_CREATE, verb="run"))

    with server.client() as http:
        command.main(["--pipe-uuid", PIPE, "--yes", "--wait"], standalone_mode=False, obj={"http": http})

    body = json.loads(capsys.readouterr().out)
    assert body["result"]["status"] == "skipped"
    assert body["wait"]["outcome"] == "not_started"
    assert server.calls("GET", "/api/cdc/backfill/runs/None") == []


def test_the_sdk_reports_a_wait_not_started(server):
    server.on("POST", "/api/cdc/backfill", answer(200, BACKFILL_SKIPPED))
    transport = Transport(server=SERVER, tokens=StaticToken(TOKEN), client_kind="sdk", http=server.client())
    resource = Resource(Session(transport))

    done = resource._call(replace(BACKFILL_RUN_CREATE, verb="run"), {"pipe_uuid": PIPE}, wait=True)

    assert done.wait.outcome == "not_started"
    assert done.result.status == "skipped"


def test_a_failed_wait_exits_5_and_still_prints_the_last_state(cli, server):
    server.on("GET", STATUS, _status("failed", last_error="bad credentials"))

    run = cli("pipe", "status", PIPE, "--wait")

    assert run.code == 5
    assert run.json()["wait"]["state"] == "failed"
    assert run.error()["code"] == "wait_failed"


PRECHECK = "/api/configstore/source/precheck"
PRECHECK_ARGS = ("source", "precheck", "postgres", "--host", "h", "--port", "5432", "--db-name", "d", "--username", "u")


def _report(ready: bool) -> dict[str, object]:
    return {
        "checked_at": "2026-10-30T00:00:00Z",
        "checks": [],
        "provider": "postgres",
        "reachable": True,
        "ready": ready,
        "report_version": 1,
        "source_type": "postgres",
    }


def test_a_not_ready_verdict_exits_9_and_prints_the_answer(cli, server, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "pw-value")
    server.on("POST", PRECHECK, answer(200, _report(ready=False)))

    run = cli(*PRECHECK_ARGS, "--password-env", "PGPASSWORD")

    assert run.code == 9
    assert run.json()["result"]["ready"] is False
    assert run.error()["code"] == "not_ready"
    sent = json.loads(server.calls("POST", PRECHECK)[0].content)
    assert sent["source_type"] == "postgres"
    assert sent["password"] == "pw-value"


def test_a_ready_verdict_exits_0(cli, server, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "pw-value")
    server.on("POST", PRECHECK, answer(200, _report(ready=True)))

    assert cli(*PRECHECK_ARGS, "--password-env", "PGPASSWORD").code == 0


WAREHOUSE = "22222222-3333-4444-5555-666666666666"
WAREHOUSE_PATH = f"/api/configstore/databases/{WAREHOUSE}"


def _warehouse(status: str) -> httpx.Response:
    return answer(
        200,
        {
            "uuid": WAREHOUSE,
            "name": "w",
            "host": "h",
            "port": 1,
            "type": "clickhouse",
            "username": "u",
            "status": status,
        },
    )


def test_a_project_scoped_wait_polls_in_the_project_and_its_next_names_it(cli, server):
    server.on("POST", f"{WAREHOUSE_PATH}/retry", _warehouse("provisioning"))
    server.on("GET", WAREHOUSE_PATH, _warehouse("provisioning"))

    run = cli("warehouse", "retry", WAREHOUSE, "--project", "proj-other", "--wait", "--timeout", "0.001")

    assert run.code == 4
    assert {r.headers["x-databay-project-id"] for r in server.calls("GET", WAREHOUSE_PATH)} == {"proj-other"}
    assert run.json()["next"] == f"databay warehouse get {WAREHOUSE} --wait --timeout 0.001 --project proj-other"


def test_resuming_a_project_scoped_wait_sends_the_project(cli, server):
    server.on("GET", WAREHOUSE_PATH, _warehouse("ready"))

    run = cli("warehouse", "get", WAREHOUSE, "--wait", "--project", "proj-other")

    assert run.code == 0, run.stderr
    assert run.json()["project"] == "proj-other"
    assert server.calls("GET", WAREHOUSE_PATH)[0].headers["x-databay-project-id"] == "proj-other"


def test_the_sdk_resumes_a_project_scoped_wait_in_the_active_project(server):
    from databay import Databay
    from databay.tokens import StaticToken

    server.on("GET", WAREHOUSE_PATH, _warehouse("ready"))
    with server.client() as http, Databay(SERVER, StaticToken(TOKEN), project="proj-sdk", http=http) as client:
        done = client.warehouse.get(db_uuid=WAREHOUSE, wait=True)

    assert done.wait.outcome == "success"
    assert done.result.status == "ready"
    assert server.calls("GET", WAREHOUSE_PATH)[0].headers["x-databay-project-id"] == "proj-sdk"


def test_a_wait_refreshes_the_token_once_on_401(server):
    class Refreshing:
        def __init__(self) -> None:
            self.value = "old"

        def token(self) -> str:
            return self.value

        def refreshed(self) -> str:
            self.value = "new"
            return self.value

    server.on(
        "GET",
        STATUS,
        lambda r: (
            _status("active")
            if r.headers["authorization"] == "Bearer new"
            else answer(401, {"detail": "x", "code": "token_invalid"})
        ),
    )
    transport = Transport(server=SERVER, tokens=Refreshing(), client_kind="sdk", http=server.client())
    wait = PIPE_CREATE.wait
    assert wait is not None

    outcome = waits.run(Session(transport), wait, {"pipe_uuid": PIPE}, timeout_s=5)

    assert outcome.outcome == "success"
