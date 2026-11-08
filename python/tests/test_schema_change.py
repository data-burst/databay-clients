"""The ``schema-change`` commands: what ``state`` and ``pending --status`` send and what their answers parse into."""

from __future__ import annotations

from conftest import PROJECT, answer

STATE_PATH = "/api/schema-evolution/pipes/state"
STOPPED = {
    "pipe_uuid": "p1",
    "state": "stopped",
    "display": "stopped",
    "next_step": "rebuild_table",
    "held_backup": False,
    "changes": [
        {
            "column": "price",
            "operation": "retype",
            "previous_type": "int",
            "new_type": "numeric",
            "detected_at": "2026-11-01T10:00:00Z",
        }
    ],
}


def test_state_filters_by_pipe_and_state_in_the_active_project(cli, server):
    server.on("GET", STATE_PATH, answer(200, [STOPPED]))

    run = cli("schema-change", "state", "--pipe-uuid", "p1", "--state", "stopped", "--output", "json")

    assert run.code == 0, run.stderr
    sent = server.calls("GET", STATE_PATH)[0]
    assert dict(sent.url.params) == {"pipe_uuid": "p1", "state": "stopped"}
    assert sent.headers["x-databay-project-id"] == PROJECT
    assert run.json()["result"][0]["next_step"] == "rebuild_table"


def test_pending_sends_each_status_as_its_own_parameter(cli, server):
    path = "/api/schema-evolution/pipe/p1/schema-change-events"
    server.on("GET", path, answer(200, []))

    run = cli("schema-change", "pending", "p1", "--status", "applied", "--status", "ignored")

    assert run.code == 0, run.stderr
    assert server.calls("GET", path)[0].url.params.get_list("status") == ["applied", "ignored"]
