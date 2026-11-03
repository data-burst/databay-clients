"""The hand-written commands that read and write local state: ``profile``, ``status``, ``project use`` and ``sql``."""

from __future__ import annotations

import csv
import io
import json
import tomllib

import pytest
from conftest import PROJECT, SERVER, answer

from databay.version import CLIENT_VERSION

ME = {"sub": "u-1", "preferred_username": "ada", "organization": ["acme", "other"], "principal": "user"}
CAPS = {"admin": False, "edit": True, "view": True}


def _project(project_id: str, name: str, status: str = "active") -> dict:
    return {
        "id": project_id,
        "name": name,
        "access": "restricted",
        "capabilities": CAPS,
        "is_general": False,
        "revision": 1,
        "role": "editor",
        "status": status,
    }


def _config(profile_env) -> dict:
    return tomllib.loads((profile_env["config"] / "config.toml").read_text(encoding="utf-8"))


def _add_profile(profile_env, name: str, server: str) -> None:
    path = profile_env["config"] / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + f'\n[profiles.{name}]\nserver = "{server}"\nauth = "env"\n',
        encoding="utf-8",
    )


def test_profile_use_switches_the_current_profile(cli, profile_env):
    _add_profile(profile_env, "prod", "https://prod.test")

    run = cli("profile", "use", "prod")

    assert run.code == 0, run.stderr
    assert _config(profile_env)["current_profile"] == "prod"


def test_profile_use_of_an_unknown_profile_changes_nothing(cli, profile_env):
    run = cli("profile", "use", "nope")

    assert run.code == 3
    assert run.error()["code"] == "no_profile"
    assert _config(profile_env)["current_profile"] == "test"


def test_profile_list_marks_the_current_profile(cli, profile_env):
    _add_profile(profile_env, "prod", "https://prod.test")

    run = cli("profile", "list")

    assert run.code == 0, run.stderr
    rows = {row["name"]: row for row in run.json()["result"]}
    assert (rows["test"]["current"], rows["prod"]["current"]) == (True, False)
    assert rows["test"]["active_project"] == PROJECT


@pytest.mark.parametrize(
    ("args", "env", "rule"),
    [
        ((), {}, "stdout is captured"),
        (("--output", "json"), {}, "--output"),
        ((), {"DATABAY_OUTPUT": "json"}, "DATABAY_OUTPUT"),
    ],
)
def test_status_names_who_where_and_the_output_rule(cli, server, monkeypatch, args, env, rule):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    server.on("GET", "/api/auth/me", answer(200, ME))

    run = cli("status", *args)

    assert run.code == 0, run.stderr
    assert run.json()["result"] == {
        "profile": "test",
        "server": SERVER,
        "product": "core",
        "api_version": "1.0",
        "auth": "env",
        "user": "ada",
        "sub": "u-1",
        "principal": "user",
        "org": "acme",
        "active_project": PROJECT,
        "output": {"format": "json", "rule": rule},
        "skill": {"state": "not_installed", "cli_version": CLIENT_VERSION, "installed": []},
    }


def test_project_use_sets_the_active_project_it_can_see(cli, server, profile_env):
    server.on("GET", "/api/workspace/projects", answer(200, [_project("p-2", "Marketing")]))

    run = cli("project", "use", "p-2")

    assert run.code == 0, run.stderr
    assert _config(profile_env)["profiles"]["test"]["active_project"] == "p-2"
    assert run.json()["result"]["name"] == "Marketing"


@pytest.mark.parametrize(
    "visible",
    [[_project("p-2", "Marketing")], [_project("p-9", "Leaving", status="deleting")]],
    ids=["not-visible", "deleting"],
)
def test_project_use_of_a_project_it_cannot_use_is_project_not_found(cli, server, profile_env, visible):
    server.on("GET", "/api/workspace/projects", answer(200, visible))

    run = cli("project", "use", "p-9")

    assert run.code == 1
    assert run.error()["code"] == "project_not_found"
    assert _config(profile_env)["profiles"]["test"]["active_project"] == PROJECT


def test_project_use_keeps_the_generated_project_verbs(cli, server):
    server.on("GET", "/api/workspace/projects", answer(200, []))

    run = cli("project", "list")

    assert run.code == 0, run.stderr


SQL_ANSWER = {
    "columns": ["n", "n"],
    "rows": [[1, "a,b"], [2, None]],
    "row_count": 2,
    "elapsed_ms": 4,
    "truncated": False,
    "project_id": PROJECT,
    "warehouse_uuid": "wh-1",
    "warnings": [],
}
SQL_PATH = "/api/configstore/sql/execute"


def test_sql_prints_the_answer_as_json(cli, server):
    server.on("POST", SQL_PATH, answer(200, SQL_ANSWER))

    run = cli("sql", "--db", "wh-1", "select 1")

    assert run.code == 0, run.stderr
    assert run.json() == {"result": SQL_ANSWER, "project": PROJECT}
    sent = server.calls("POST", SQL_PATH)[0]
    assert json.loads(sent.content) == {"query": "select 1", "db_uuid": "wh-1"}
    assert sent.headers["x-databay-project-id"] == PROJECT


def test_sql_csv_keeps_repeated_columns_and_whole_values(cli, server):
    server.on("POST", SQL_PATH, answer(200, SQL_ANSWER))

    run = cli("sql", "--output", "csv", "select 1")

    assert run.code == 0, run.stderr
    assert list(csv.reader(io.StringIO(run.stdout))) == [["n", "n"], ["1", "a,b"], ["2", ""]]


def test_sql_table_prints_the_rows_under_their_columns(cli, server):
    server.on("POST", SQL_PATH, answer(200, SQL_ANSWER))

    run = cli("sql", "--output", "table", "select 1")

    assert run.code == 0, run.stderr
    assert run.stdout.splitlines()[0] == f"project: {PROJECT}"
    assert "a,b" in run.stdout


def test_sql_reads_the_statement_from_a_file(cli, server, tmp_path):
    server.on("POST", SQL_PATH, answer(200, SQL_ANSWER))
    statement = tmp_path / "q.sql"
    statement.write_text("select 2\n", encoding="utf-8")

    run = cli("sql", "--file", str(statement))

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("POST", SQL_PATH)[0].content) == {"query": "select 2"}


def test_sql_reads_the_statement_from_stdin(cli, server, monkeypatch):
    server.on("POST", SQL_PATH, answer(200, SQL_ANSWER))

    run = cli("sql", stdin="select 3", monkeypatch=monkeypatch)

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("POST", SQL_PATH)[0].content) == {"query": "select 3"}


def test_sql_with_both_an_argument_and_a_file_is_a_usage_error(cli, server, tmp_path):
    run = cli("sql", "--file", str(tmp_path / "q.sql"), "select 1")

    assert run.code == 2
    assert run.error()["code"] == "usage_invalid"
    assert server.requests == []


def test_sql_says_when_the_rows_were_truncated(cli, server):
    server.on("POST", SQL_PATH, answer(200, {**SQL_ANSWER, "truncated": True}))

    run = cli("sql", "--output", "csv", "select 1")

    assert run.code == 0, run.stderr
    assert run.events("truncated")
