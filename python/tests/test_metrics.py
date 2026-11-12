"""``databay metrics``: the generated list, the hand-written describe and query, and the ``--where`` parser."""

from __future__ import annotations

import csv
import io
import json
import re
import shlex

import pytest
from conftest import PROJECT, answer

from databay.agent_skill import SOURCE
from databay.cli.metrics import parse_order, parse_where
from databay.errors import DatabayUsageError

BASE = f"/api/semantic/projects/{PROJECT}/metrics"
NET_SALES = {
    "name": "net_sales",
    "description": "Gross sales less discounts and returns.",
    "type": "sum",
    "state": "ready",
    "reason": None,
    "warehouses": ["wh-1"],
    "dimensions": [],
    "time_dimension": "day",
    "grains": ["day", "week", "month", "quarter", "year"],
    "filter_ops": ["equals", "notEquals", "gt", "gte", "lt", "lte", "set", "notSet"],
}
LISTED = {"model_version": 3, "policy_revision": "r-1", "state": "ready", "reason": None, "metrics": [NET_SALES]}
ROWS = {
    "columns": [{"name": "day", "type": "time"}, {"name": "net_sales", "type": "number"}],
    "rows": [["2026-11-08", 120.5], ["2026-11-09", 98.0]],
    "row_count": 2,
    "truncated": False,
    "answer_path": "metric",
    "model_version": 3,
    "policy_revision": "r-1",
    "warehouse": "wh-1",
    "dry_run": False,
    "explain": None,
}
EXPLAIN = {
    "definition": {
        "name": "net_sales",
        "description": "Gross sales less discounts and returns.",
        "type": "sum",
        "expression": "gross_sales - discounts - returns",
        "cube": "shopify_sales",
        "members": ["gross_sales", "discounts", "returns"],
    },
    "sql": {"text": "SELECT day, sum(net) FROM sales GROUP BY day", "params": []},
    "lineage": {"warehouse": "wh-1", "views": [{"database": "analytics", "name": "sales"}], "tables": []},
}


def _sent(server) -> dict:
    return json.loads(server.calls("POST", f"{BASE}/query")[-1].content)


def test_the_hello_world_sequence_run_by_an_agent_lists_queries_and_explains_as_agent(cli, server):
    server.on("GET", BASE, answer(200, LISTED))
    server.on("POST", f"{BASE}/query", answer(200, ROWS), answer(200, {**ROWS, "explain": EXPLAIN}))

    listed = cli("metrics", "list")
    queried = cli("metrics", "query", "net_sales", "--grain", "day", "--last", "30d", "--caller", "agent")
    explained = cli(
        "metrics", "query", "net_sales", "--grain", "day", "--last", "30d", "--explain", "--caller", "agent"
    )

    assert (listed.code, queried.code, explained.code) == (0, 0, 0), explained.stderr
    assert [m["name"] for m in listed.json()["result"]["metrics"]] == ["net_sales"]
    assert queried.json() == {"result": ROWS, "project": PROJECT}
    assert explained.json()["result"]["explain"]["sql"]["text"].startswith("SELECT")
    first, second = (json.loads(r.content) for r in server.calls("POST", f"{BASE}/query"))
    assert first == {"metric": "net_sales", "explain": False, "dry_run": False, "time": {"grain": "day", "last": "30d"}}
    assert second == {**first, "explain": True}
    assert {r.headers["x-databay-caller"] for r in server.calls("POST", f"{BASE}/query")} == {"agent"}


def test_a_query_without_caller_is_counted_as_cli(cli, server):
    server.on("POST", f"{BASE}/query", answer(200, ROWS))

    run = cli("metrics", "query", "net_sales")

    assert run.code == 0, run.stderr
    assert server.calls("POST", f"{BASE}/query")[-1].headers["x-databay-caller"] == "cli"


def test_an_unknown_caller_is_a_usage_error_before_any_request(cli, server):
    run = cli("metrics", "query", "net_sales", "--caller", "bot")

    assert run.code == 2
    assert run.error()["code"] == "usage_invalid"
    assert server.calls("POST", f"{BASE}/query") == []


def test_the_skills_worked_example_runs_against_the_api(cli, server):
    text = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
    block = re.search(r"### Worked example\n.*?```\n(.*?)```", text, re.DOTALL)
    assert block is not None
    server.on("GET", BASE, answer(200, LISTED))
    server.on("GET", f"{BASE}/net_sales", answer(200, NET_SALES))
    server.on("POST", f"{BASE}/query", answer(200, ROWS), answer(200, {**ROWS, "explain": EXPLAIN}))

    runs = [cli(*shlex.split(line)[1:]) for line in block.group(1).splitlines()]

    assert [run.code for run in runs] == [0] * 4, [run.stderr for run in runs]
    assert runs[-1].json()["result"]["explain"] == EXPLAIN
    assert {r.headers["x-databay-caller"] for r in server.calls("POST", f"{BASE}/query")} == {"agent"}


def test_query_sends_every_flag_in_the_body(cli, server):
    server.on("POST", f"{BASE}/query", answer(200, {**ROWS, "dry_run": True, "rows": [], "row_count": 0}))

    run = cli(
        "metrics", "query", "channel_sessions",
        "--by", "channel", "--by", "day",
        "--from", "2026-10-01", "--to", "2026-10-31",
        "--where", "channel=Paid Search", "--where", "day>=2026-10-15",
        "--order", "channel_sessions:desc", "--order", "channel",
        "--limit", "50", "--warehouse", "wh-2", "--model-version", "3", "--dry-run",
    )  # fmt: skip

    assert run.code == 0, run.stderr
    assert _sent(server) == {
        "metric": "channel_sessions",
        "explain": False,
        "dry_run": True,
        "dimensions": ["channel", "day"],
        "time": {"from": "2026-10-01", "to": "2026-10-31"},
        "filters": [
            {"member": "channel", "op": "equals", "values": ["Paid Search"]},
            {"member": "day", "op": "gte", "values": ["2026-10-15"]},
        ],
        "order": [{"member": "channel_sessions", "direction": "desc"}, {"member": "channel", "direction": "asc"}],
        "limit": 50,
        "warehouse": "wh-2",
        "model_version": 3,
    }


def test_query_acts_in_the_project_named_by_project(cli, server):
    server.on("POST", "/api/semantic/projects/p-2/metrics/query", answer(200, ROWS))

    run = cli("metrics", "query", "net_sales", "--project", "p-2")

    assert run.code == 0, run.stderr
    assert run.json()["project"] == "p-2"


def test_query_table_prints_the_rows_under_their_columns_and_the_sql(cli, server):
    server.on("POST", f"{BASE}/query", answer(200, {**ROWS, "explain": EXPLAIN}))

    run = cli("metrics", "query", "net_sales", "--grain", "day", "--explain", "--output", "table")

    assert run.code == 0, run.stderr
    lines = run.stdout.splitlines()
    assert lines[0] == f"project: {PROJECT}"
    assert any("day" in line and "net_sales" in line for line in lines)
    assert any("120.5" in line for line in lines)
    assert "SELECT day, sum(net) FROM sales GROUP BY day" in run.stdout


def test_query_csv_is_the_rows_alone(cli, server):
    server.on("POST", f"{BASE}/query", answer(200, ROWS))

    run = cli("metrics", "query", "net_sales", "--grain", "day", "--output", "csv")

    assert run.code == 0, run.stderr
    assert list(csv.reader(io.StringIO(run.stdout))) == [
        ["day", "net_sales"],
        ["2026-11-08", "120.5"],
        ["2026-11-09", "98.0"],
    ]


def test_query_says_when_the_rows_were_truncated(cli, server):
    server.on("POST", f"{BASE}/query", answer(200, {**ROWS, "truncated": True}))

    run = cli("metrics", "query", "net_sales", "--output", "csv")

    assert run.code == 0, run.stderr
    assert run.events("truncated")


def test_describe_names_the_metric_in_the_path(cli, server):
    server.on("GET", f"{BASE}/net_sales", answer(200, NET_SALES))

    run = cli("metrics", "describe", "net_sales")

    assert run.code == 0, run.stderr
    assert run.json() == {"result": NET_SALES, "project": PROJECT}


def test_a_metric_not_found_carries_did_you_mean(cli, server):
    body = {
        "detail": "no metric net_sale in this project",
        "code": "metric_not_found",
        "metric": "net_sale",
        "model_version": 3,
        "did_you_mean": ["net_sales"],
    }
    server.on("POST", f"{BASE}/query", answer(404, body))

    run = cli("metrics", "query", "net_sale")

    assert run.code == 1
    error = run.error()
    assert error["code"] == "metric_not_found"
    assert error["details"]["did_you_mean"] == ["net_sales"]


def test_a_bad_where_is_a_usage_error_before_any_request(cli, server):
    run = cli("metrics", "query", "net_sales", "--where", "channel")

    assert run.code == 2
    assert run.error()["code"] == "usage_invalid"
    assert server.calls("POST", f"{BASE}/query") == []


@pytest.mark.parametrize(
    ("text", "member", "op", "value"),
    [
        ("channel=paid", "channel", "equals", "paid"),
        ("channel!=paid", "channel", "notEquals", "paid"),
        ("orders>=10", "orders", "gte", "10"),
        ("orders<=10", "orders", "lte", "10"),
        ("orders>10", "orders", "gt", "10"),
        ("orders<10", "orders", "lt", "10"),
        ("channel=Paid Search, EU", "channel", "equals", "Paid Search, EU"),
        ("note=a=b", "note", "equals", "a=b"),
        (" channel = Paid Search ", "channel", "equals", "Paid Search"),
    ],
)
def test_where_parses_into_a_filter(text, member, op, value):
    assert parse_where(text) == {"member": member, "op": op, "values": [value]}


@pytest.mark.parametrize(
    "text",
    ["channel", "channel=", "channel= ", "=paid", "Channel=paid", "1st=x", "channel~paid", "a==b", "a=>5", "a<>b"],
)
def test_where_refuses_what_is_not_member_op_value(text):
    with pytest.raises(DatabayUsageError):
        parse_where(text)


@pytest.mark.parametrize(
    ("text", "member", "direction"),
    [("net_sales", "net_sales", "asc"), ("net_sales:desc", "net_sales", "desc"), ("day:asc", "day", "asc")],
)
def test_order_parses_into_an_order(text, member, direction):
    assert parse_order(text) == {"member": member, "direction": direction}


@pytest.mark.parametrize("text", ["", "net_sales:down", "-net_sales"])
def test_order_refuses_what_is_not_member_and_direction(text):
    with pytest.raises(DatabayUsageError):
        parse_order(text)


def test_csv_says_the_explain_is_only_in_json(cli, server):
    server.on("POST", f"{BASE}/query", answer(200, {**ROWS, "explain": EXPLAIN}))

    run = cli("metrics", "query", "net_sales", "--explain", "--output", "csv")

    assert run.code == 0, run.stderr
    assert run.events("explain")
    assert "SELECT" not in run.stdout


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _terminal_error(status: int) -> str:
    from databay.cli.console import Console

    stderr = Terminal()
    console = Console(output="table", stdout=Terminal(), stderr=stderr)
    console.error(
        {"code": "metric_query_failed", "message": "failed", "status": status, "details": {"trace_id": "abc123"}}
    )
    return stderr.getvalue()


def test_a_terminal_sees_the_trace_id_of_a_5xx():
    assert "trace_id: abc123" in _terminal_error(502)


def test_a_terminal_sees_no_trace_id_line_for_a_4xx():
    assert "trace_id" not in _terminal_error(422)


def test_captured_stderr_carries_the_trace_id_of_a_5xx(cli, server):
    body = {"detail": "the warehouse failed the query", "code": "metric_query_failed", "trace_id": "abc123"}
    server.on("POST", f"{BASE}/query", answer(502, body))

    run = cli("metrics", "query", "net_sales", "--no-retry")

    assert run.error()["details"]["trace_id"] == "abc123"


def test_captured_stderr_carries_the_trace_id_of_a_5xx_with_a_code_this_cli_does_not_know(cli, server):
    body = {"detail": "newer refusal", "code": "some_future_code", "trace_id": "abc123"}
    server.on("POST", f"{BASE}/query", answer(503, body))

    run = cli("metrics", "query", "net_sales", "--no-retry")

    assert run.error()["details"]["trace_id"] == "abc123"
