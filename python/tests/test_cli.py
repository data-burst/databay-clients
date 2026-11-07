"""The command line's own rules: confirmation, secrets, output, the project, profiles and versions."""

from __future__ import annotations

import io
import json
import os
import pty
import select
import subprocess
import sys
import time

import httpx
import pytest
from conftest import PROJECT, SERVER, TOKEN, answer

from databay.cli.main import main

PIPE = "11111111-2222-3333-4444-555555555555"


def test_a_destructive_command_needs_yes_when_output_is_captured(cli, server):
    run = cli("pipe", "delete", PIPE)

    assert run.code == 2
    assert run.error()["code"] == "confirmation_required"
    assert server.requests == []


def test_yes_sends_a_destructive_command(cli, server):
    server.on("DELETE", f"/api/cdc/pipe/{PIPE}", answer(204))

    run = cli("pipe", "delete", PIPE, "--yes")

    assert run.code == 0, run.stderr
    assert run.json() == {"result": None}
    assert len(server.calls("DELETE", f"/api/cdc/pipe/{PIPE}")) == 1


def _run_on_a_terminal(args: list[str], reply: str, env: dict[str, str]) -> tuple[int, str]:
    """Run the CLI with a real terminal as stdin, stdout and stderr; answer its prompt with ``reply``."""
    leader, follower = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "databay", *args], stdin=follower, stdout=follower, stderr=follower, env=env
    )
    os.close(follower)
    seen = b""
    deadline = time.monotonic() + 30
    answered = False
    while time.monotonic() < deadline:
        ready, _, _ = select.select([leader], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(leader, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
        if not answered and b"[y/N]" in seen:
            os.write(leader, reply.encode() + b"\r")
            answered = True
        if process.poll() is not None and not ready:
            break
    process.wait(timeout=30)
    os.close(leader)
    return process.returncode, seen.decode(errors="replace")


@pytest.fixture
def unreachable_profile(tmp_path) -> dict[str, str]:
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text(
        'current_profile = "dead"\n[profiles.dead]\nserver = "http://127.0.0.1:9"\nauth = "env"\n', encoding="utf-8"
    )
    return {
        **os.environ,
        "DATABAY_CONFIG_DIR": str(config),
        "DATABAY_STATE_DIR": str(tmp_path / "state"),
        "DATABAY_TOKEN": TOKEN,
        "NO_COLOR": "1",
    }


def test_on_a_terminal_anything_but_y_cancels_with_exit_1(unreachable_profile):
    code, screen = _run_on_a_terminal(["pipe", "delete", PIPE], "n", unreachable_profile)

    assert f"delete pipe {PIPE}? [y/N]" in screen
    assert code == 1
    assert "cancelled" in screen


def test_on_a_terminal_y_goes_ahead(unreachable_profile):
    code, screen = _run_on_a_terminal(["pipe", "delete", PIPE], "y", unreachable_profile)

    # The server is unreachable, so going ahead ends in network_error: the command did not stop at the prompt.
    assert code == 6
    assert "network_error" in screen


def test_a_secret_is_never_a_value_flag(cli):
    run = cli("source", "precheck", "postgres", "--help")

    assert "--password-env NAME" in run.stdout
    assert "--password-stdin" in run.stdout
    assert "--password " not in run.stdout


ACCOUNTS = {"provider": "instagram", "accounts": []}


def test_a_secret_is_read_from_stdin(cli, server, monkeypatch):
    server.on("POST", "/api/configstore/instagram/list-accounts", answer(200, ACCOUNTS))

    run = cli("instagram-account", "list", "--access-token-stdin", stdin="ig-token-value\n", monkeypatch=monkeypatch)

    assert run.code == 0, run.stderr
    sent = server.calls("POST", "/api/configstore/instagram/list-accounts")[0]
    assert json.loads(sent.content) == {"access_token": "ig-token-value"}


def test_a_json_field_that_holds_a_secret_is_read_like_a_secret(cli):
    run = cli("canvas", "activate", "--help")

    assert "--secrets-env NAME" in run.stdout
    assert "--secrets TEXT" not in run.stdout


def test_a_required_secret_with_no_source_off_a_terminal_is_secret_required(cli, server, monkeypatch):
    run = cli("instagram-account", "list", stdin="", monkeypatch=monkeypatch)

    assert run.code == 2
    error = run.error()
    assert error["code"] == "secret_required"
    assert "--access-token-env" in error["message"]
    assert server.requests == []


def test_an_unset_secret_variable_is_secret_required(cli, server, monkeypatch):
    monkeypatch.delenv("IG_TOKEN", raising=False)

    run = cli("instagram-account", "list", "--access-token-env", "IG_TOKEN")

    assert run.code == 2
    assert run.error()["code"] == "secret_required"
    assert server.requests == []


def test_verbose_output_never_carries_a_secret_or_the_token(cli, server, monkeypatch):
    monkeypatch.setenv("IG_TOKEN", "ig-very-secret-value")
    server.on("POST", "/api/configstore/instagram/list-accounts", answer(200, ACCOUNTS))

    run = cli("instagram-account", "list", "--access-token-env", "IG_TOKEN", "--verbose")

    assert run.code == 0, run.stderr
    assert run.events("request"), run.stderr
    assert "ig-very-secret-value" not in run.stderr + run.stdout
    assert TOKEN not in run.stderr + run.stdout
    logged = next(e for e in run.events("request") if e["url"].endswith("/list-accounts"))
    assert logged["body"] == {"access_token": "<redacted>"}
    assert logged["headers"]["Authorization"] == "<redacted>"


CATALOG = [{"type": "postgres", "display_name": "Postgres", "auth_kind": "static_credentials", "fields": []}]


def test_captured_output_is_json(cli, server):
    server.on("GET", "/api/configstore/source-catalog", answer(200, CATALOG))

    run = cli("source-catalog", "list")

    assert run.code == 0, run.stderr
    assert run.json() == {"result": CATALOG}


def test_databay_output_picks_csv_with_the_declared_columns(cli, server, monkeypatch):
    server.on("GET", "/api/configstore/source-catalog", answer(200, CATALOG))
    monkeypatch.setenv("DATABAY_OUTPUT", "csv")

    run = cli("source-catalog", "list")

    assert run.stdout.splitlines() == ["type,display_name,auth_kind", "postgres,Postgres,static_credentials"]


def test_output_flag_wins_over_databay_output(cli, server, monkeypatch):
    server.on("GET", "/api/configstore/source-catalog", answer(200, CATALOG))
    monkeypatch.setenv("DATABAY_OUTPUT", "csv")

    run = cli("source-catalog", "list", "--output", "json")

    assert run.json() == {"result": CATALOG}


def test_an_invalid_databay_output_is_a_usage_error(cli, monkeypatch):
    monkeypatch.setenv("DATABAY_OUTPUT", "yaml")

    run = cli("pipe", "get", PIPE)

    assert run.code == 2
    assert run.error()["code"] == "usage_invalid"


def test_the_active_project_is_sent_where_the_route_takes_it(cli, server):
    server.on("GET", "/api/cdc/pipe", answer(200, []))

    run = cli("pipe", "list")

    assert run.code == 0, run.stderr
    assert server.calls("GET", "/api/cdc/pipe")[0].url.params["project_id"] == PROJECT
    assert run.json()["project"] == PROJECT


def test_project_flag_wins_over_the_active_project(cli, server):
    server.on("GET", "/api/cdc/pipe", answer(200, []))

    cli("pipe", "list", "--project", "proj-flag")

    assert server.calls("GET", "/api/cdc/pipe")[0].url.params["project_id"] == "proj-flag"


def test_an_explicit_project_argument_wins_over_the_active_project(cli, server):
    server.on("GET", "/api/workspace/projects/proj-arg", answer(200, {}))

    cli("project", "get", "proj-arg")

    assert server.calls("GET", "/api/workspace/projects/proj-arg")


def test_an_explicit_project_and_a_different_flag_conflict(cli, server):
    run = cli("project", "get", "proj-arg", "--project", "proj-flag")

    assert run.code == 2
    assert run.error()["code"] == "project_conflict"
    assert server.requests == []


def test_no_project_anywhere_is_no_active_project(cli, server, profile_env):
    (profile_env["config"] / "config.toml").write_text(
        'current_profile = "test"\n[profiles.test]\nserver = "http://databay.test"\nauth = "env"\n', encoding="utf-8"
    )

    run = cli("pipe", "list")

    assert run.code == 2
    assert run.error()["code"] == "no_active_project"
    assert server.requests == []


BACKFILL = {"run_uuid": "r1", "pipe_uuid": PIPE, "status": "started"}


def test_a_create_sends_a_new_idempotency_key_and_prints_it(cli, server):
    server.on("POST", "/api/cdc/backfill", answer(200, BACKFILL))

    first = cli("backfill-run", "create", "--pipe-uuid", PIPE, "--yes")
    second = cli("backfill-run", "create", "--pipe-uuid", PIPE, "--yes")

    assert first.code == 0, first.stderr
    sent = [r.headers["idempotency-key"] for r in server.calls("POST", "/api/cdc/backfill")]
    assert sent == [first.json()["idempotency_key"], second.json()["idempotency_key"]]
    assert sent[0] != sent[1]
    assert first.json()["next"] == "databay backfill-run get r1 --wait"


def test_idempotency_key_reuses_a_key(cli, server):
    server.on("POST", "/api/cdc/backfill", answer(200, BACKFILL))

    cli("backfill-run", "create", "--pipe-uuid", PIPE, "--idempotency-key", "key-1", "--yes")

    assert server.calls("POST", "/api/cdc/backfill")[0].headers["idempotency-key"] == "key-1"


def test_no_profile_exits_3(cli, server, profile_env):
    (profile_env["config"] / "config.toml").unlink()

    run = cli("pipe", "get", PIPE)

    assert run.code == 3
    assert run.error()["code"] == "no_profile"


def test_an_unknown_profile_key_is_an_error_not_ignored(cli, profile_env):
    path = profile_env["config"] / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + 'token = "x"\n', encoding="utf-8")

    run = cli("pipe", "get", PIPE)

    assert run.code == 3
    assert run.error()["code"] == "profile_invalid"


def test_an_env_profile_without_databay_token_is_token_missing(cli, server, monkeypatch):
    monkeypatch.delenv("DATABAY_TOKEN")

    run = cli("pipe", "get", PIPE)

    assert run.code == 3
    assert run.error()["code"] == "token_missing"
    assert server.requests == []


@pytest.mark.parametrize(
    ("server_config", "code"),
    [
        ({"api_version": "2.0"}, "server_version_mismatch"),
        ({"product": "shopify"}, "unsupported_product"),
    ],
)
def test_a_server_this_cli_cannot_talk_to_exits_7_before_the_command_is_sent(cli, server, server_config, code):
    server.client_config.update(server_config)

    run = cli("pipe", "get", PIPE)

    assert run.code == 7
    assert run.error()["code"] == code
    assert server.calls("GET", f"/api/cdc/pipe/{PIPE}") == []


def test_server_too_old_names_the_command_and_both_versions(server):
    from dataclasses import replace

    from databay import compat
    from databay.errors import DatabayVersionError
    from databay.operations.pipe import PIPE_GET

    newer = replace(PIPE_GET, since="1.4")
    info = compat.ServerInfo(product="core", api_version="1.3", issuer="i", client_id="c")

    with pytest.raises(DatabayVersionError) as caught:
        compat.check(info, newer)

    assert caught.value.code == "server_too_old"
    assert caught.value.exit_code == 7
    assert "databay pipe get" in caught.value.message
    assert "1.4" in caught.value.message
    assert "1.3" in caught.value.message


def test_the_client_config_is_cached_between_commands(cli, server):
    server.on(
        "GET",
        f"/api/cdc/pipe/{PIPE}/status",
        answer(200, {"pipe_uuid": PIPE, "pipe_status": "active", "health": "healthy"}),
    )

    cli("pipe", "status", PIPE)
    cli("pipe", "status", PIPE)

    assert len(server.calls("GET", "/api/auth/client-config")) == 1


def test_windows_is_unsupported_platform():
    from databay.errors import DatabayUsageError
    from databay.platform_check import ensure_supported

    with pytest.raises(DatabayUsageError) as caught:
        ensure_supported("win32")

    assert caught.value.code == "unsupported_platform"
    assert caught.value.exit_code == 2
    assert "WSL" in caught.value.message


def test_a_usage_error_is_the_error_object_with_exit_2(cli):
    run = cli("pipe", "get")

    assert run.code == 2
    assert run.error()["code"] == "usage_invalid"


def test_help_exits_0(capsys):
    assert main(["pipe", "get", "--help"]) == 0
    assert "databay pipe get <pipe_uuid>" in capsys.readouterr().out


def test_csv_keeps_a_long_value_whole(cli, server, monkeypatch):
    long_name = "x" * 300
    server.on("GET", "/api/configstore/source-catalog", answer(200, [{**CATALOG[0], "display_name": long_name}]))
    monkeypatch.setenv("DATABAY_OUTPUT", "csv")

    run = cli("source-catalog", "list")

    assert run.stdout.splitlines()[1] == f"postgres,{long_name},static_credentials"


PIPE_CREATED = {
    "uuid": PIPE,
    "status": "pending",
    "source_id": "s1",
    "sink_db_uuid": "d1",
    "backend": "ingest",
    "preflight_skipped": [],
}


def _pipe_file(tmp_path, project: str) -> str:
    path = tmp_path / "pipe.json"
    path.write_text(
        json.dumps({"source_id": "s1", "table_name": "t", "dest_table_name": "t", "project_id": project}),
        encoding="utf-8",
    )
    return str(path)


def test_a_body_files_project_is_the_explicit_project(cli, server, tmp_path):
    server.on("POST", "/api/cdc/pipe", answer(200, PIPE_CREATED))

    run = cli("pipe", "create", "--file", _pipe_file(tmp_path, "proj-file"))

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("POST", "/api/cdc/pipe")[0].content)["project_id"] == "proj-file"
    assert run.json()["project"] == "proj-file"


def test_a_body_files_project_and_a_different_project_flag_conflict(cli, server, tmp_path):
    run = cli("pipe", "create", "--file", _pipe_file(tmp_path, "proj-file"), "--project", "proj-flag")

    assert run.code == 2
    assert run.error()["code"] == "project_conflict"
    assert server.requests == []


def test_an_interrupt_after_sending_is_interrupted_and_names_the_key(cli, server):
    def interrupt(_request):
        raise KeyboardInterrupt

    server.on("POST", "/api/cdc/pipe", interrupt)

    run = cli("pipe", "create", "--source-id", "s1", "--table-name", "t", "--dest-table-name", "t")

    assert run.code == 1
    error = run.error()
    assert error["code"] == "interrupted"
    assert error["details"]["idempotency_key"] == server.calls("POST", "/api/cdc/pipe")[0].headers["idempotency-key"]


def test_a_refusal_from_a_cached_client_config_reads_the_server_again(cli, server):
    server.on(
        "GET",
        f"/api/cdc/pipe/{PIPE}/status",
        answer(200, {"pipe_uuid": PIPE, "pipe_status": "active", "health": "healthy"}),
    )
    server.client_config["api_version"] = "2.0"
    assert cli("pipe", "status", PIPE).code == 7

    server.client_config["api_version"] = "1.0"
    run = cli("pipe", "status", PIPE)

    assert run.code == 0, run.stderr
    assert len(server.calls("GET", "/api/auth/client-config")) == 2


def test_a_server_without_client_config_is_server_too_old(cli, server):
    server.on("GET", "/api/auth/client-config", answer(404, {"detail": "Not Found"}))

    run = cli("pipe", "get", PIPE)

    assert run.code == 7
    assert run.error()["code"] == "server_too_old"


def test_a_secret_flag_with_file_is_a_usage_error(cli, server, tmp_path, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "pw")
    body = tmp_path / "body.json"
    body.write_text('{"host": "h", "port": 5432, "db_name": "d", "username": "u"}', encoding="utf-8")

    run = cli("source", "precheck", "postgres", "--file", str(body), "--password-env", "PGPASSWORD")

    assert run.code == 2
    assert "--password-env" in run.error()["message"]
    assert server.requests == []


KEY = {
    "id": "k1",
    "name": "ci",
    "role": "reader",
    "status": "active",
    "project_ids": ["p1"],
    "created_at": "2026-10-30T00:00:00Z",
    "created_by": "u",
    "client_id": "apikey-1",
    "revoked_at": None,
    "rotated_at": None,
}


def test_csv_of_an_answer_that_holds_its_list_prints_the_lists_rows(cli, server):
    server.on("GET", "/api/auth/api-keys", answer(200, {"keys": [KEY]}))

    run = cli("api-key", "list", "--output", "csv")

    assert run.code == 0, run.stderr
    assert run.stdout.splitlines() == [
        "id,name,role,status,project_ids,created_at",
        'k1,ci,reader,active,"[""p1""]",2026-10-30T00:00:00Z',
    ]


def test_csv_reports_the_idempotency_key_and_next_on_stderr(cli, server):
    server.on("POST", "/api/cdc/backfill", answer(200, BACKFILL))

    run = cli("backfill-run", "create", "--pipe-uuid", PIPE, "--yes", "--output", "csv")

    assert run.code == 0, run.stderr
    key = server.calls("POST", "/api/cdc/backfill")[0].headers["idempotency-key"]
    assert run.events("idempotency_key") == [{"event": "idempotency_key", "value": key}]
    assert run.events("next") == [{"event": "next", "value": "databay backfill-run get r1 --wait"}]


def test_an_interrupt_before_the_command_is_sent_is_cancelled(cli, server):
    def interrupt(_request):
        raise KeyboardInterrupt

    server.on("GET", "/api/auth/client-config", interrupt)

    run = cli("pipe", "delete", PIPE, "--yes")

    assert run.code == 1
    assert run.error()["code"] == "cancelled"
    assert server.calls("DELETE", f"/api/cdc/pipe/{PIPE}") == []


def test_an_interrupted_read_is_stopped_with_nothing_changed(cli, server):
    def interrupt(_request):
        raise KeyboardInterrupt

    server.on("GET", f"/api/cdc/pipe/{PIPE}", interrupt)

    run = cli("pipe", "get", PIPE)

    error = run.error()
    assert error["code"] == "stopped"
    assert error["recovery"] == {"action": "retry", "side_effects": "none", "max_retries": None}


def test_a_project_id_flag_and_a_body_file_are_not_both_taken(cli, server, tmp_path):
    run = cli("pipe", "create", "--project-id", "proj-flag", "--file", _pipe_file(tmp_path, "proj-file"))

    assert run.code == 2
    assert "--project-id" in run.error()["message"]
    assert server.requests == []


def test_a_null_project_in_a_body_file_leaves_the_active_project(cli, server, tmp_path):
    server.on("POST", "/api/cdc/pipe", answer(200, PIPE_CREATED))
    path = tmp_path / "pipe.json"
    path.write_text(
        '{"source_id": "s1", "table_name": "t", "dest_table_name": "t", "project_id": null}', encoding="utf-8"
    )

    run = cli("pipe", "create", "--file", str(path))

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("POST", "/api/cdc/pipe")[0].content)["project_id"] == PROJECT


def test_a_refusal_from_a_cached_answer_kept_by_a_session_reads_the_server_again(server, profile_env):
    from dataclasses import replace

    from databay import compat
    from databay.operations.pipe import PIPE_GET
    from databay.tokens import StaticToken
    from databay.transport import Transport

    server.client_config["api_version"] = "1.1"
    transport = Transport(server=SERVER, tokens=StaticToken(TOKEN), client_kind="sdk", http=server.client())
    cached = (compat.ServerInfo(product="core", api_version="1.0", issuer="i", client_id="c"), True)

    info, from_cache = compat.ensure_compatible(transport, cached, replace(PIPE_GET, since="1.1"))

    assert info.api_version == "1.1"
    assert from_cache is False


def test_a_non_json_408_is_transient(cli, server):
    server.on("GET", f"/api/cdc/pipe/{PIPE}", httpx.Response(408, text="<html>timeout</html>"))

    run = cli("pipe", "get", PIPE, "--no-retry")

    assert run.code == 6
    assert run.error()["code"] == "server_unavailable"


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_a_terminal_sees_next_once_when_the_error_repeats_it():
    from databay.cli.console import Console

    stderr = Terminal()
    console = Console(output="table", stdout=Terminal(), stderr=stderr)

    console.notice("next", "databay pipe status p --wait", only_on_terminal=False)
    console.error(
        {"code": "wait_pending", "message": "still creating", "hint": "", "next": "databay pipe status p --wait"}
    )

    assert stderr.getvalue().count("next: databay pipe status p --wait") == 1
