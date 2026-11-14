"""Every answer that is not a success becomes one named error, with its exit code and recovery."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import PROJECT, SERVER, TOKEN, answer

from databay.error_catalog import EXIT_CODES, LOCAL_CODES, SERVER_EXIT_OVERRIDES, UNCODED_404_HINT, server_exit
from databay.error_codes import EXCEPTIONS, REGISTRY
from databay.errors import DatabayFeatureDisabled
from databay.feature_header import FEATURE_SOURCE_HINTS
from databay.sdk import Databay
from databay.tokens import StaticToken
from databay.version import CLIENT_VERSION

PIPE = "11111111-2222-3333-4444-555555555555"
PIPE_PATH = f"/api/cdc/pipe/{PIPE}"


def test_every_local_code_has_an_exit_the_table_names_and_a_hint():
    for code, local in LOCAL_CODES.items():
        assert local.exit_code in EXIT_CODES, code
        assert local.server_code or local.hint.strip(), code


def test_a_server_code_exits_by_its_registry_action_unless_overridden():
    retry = next(
        code for code, info in REGISTRY.items() if info.action == "retry" and code not in SERVER_EXIT_OVERRIDES
    )
    refused = next(code for code, info in REGISTRY.items() if info.action == "fix_and_retry")
    assert server_exit(retry, REGISTRY[retry].action) == 6
    assert server_exit(refused, REGISTRY[refused].action) == 1
    assert server_exit("token_invalid", REGISTRY["token_invalid"].action) == 3
    assert server_exit("project_conflict", REGISTRY["project_conflict"].action) == 2


def test_every_server_code_has_an_exception_class_named_for_it():
    assert set(EXCEPTIONS) == set(REGISTRY)
    assert EXCEPTIONS["pipe_not_found"].__name__ == "PipeNotFoundError"
    assert EXCEPTIONS["pipe_not_found"]("x").exit_code == 1


@pytest.mark.parametrize(
    ("response", "code", "exit_code"),
    [
        (answer(404, {"detail": "Pipe not found", "code": "pipe_not_found"}), "pipe_not_found", 1),
        (answer(401, {"detail": "Could not validate credentials", "code": "token_invalid"}), "token_invalid", 3),
        (answer(409, {"detail": "busy", "code": "a_code_from_a_newer_server"}), "unknown_error_code", 8),
        (answer(500, {"detail": "boom"}), "uncoded_error", 8),
        (
            httpx.Response(502, text="<html>Bad Gateway</html>", headers={"content-type": "text/html"}),
            "server_unavailable",
            6,
        ),
        (
            httpx.Response(404, text="<html>Not Found</html>", headers={"content-type": "text/html"}),
            "non_databay_answer",
            1,
        ),
    ],
)
def test_an_error_answer_maps_to_its_code_and_exit(cli, server, response, code, exit_code):
    server.on("GET", PIPE_PATH, response)

    run = cli("pipe", "get", PIPE, "--no-retry")

    assert run.code == exit_code
    assert run.error()["code"] == code
    assert run.stdout == ""


def test_a_non_json_error_body_is_never_printed(cli, server):
    server.on("GET", PIPE_PATH, httpx.Response(503, text="maintenance: secret-internal-hostname"))

    run = cli("pipe", "get", PIPE, "--no-retry")

    assert run.error()["code"] == "server_unavailable"
    assert "secret-internal-hostname" not in run.stderr


def test_the_error_object_carries_recovery_from_the_registry_and_context(cli, server):
    server.on(
        "GET",
        PIPE_PATH,
        answer(404, {"detail": "Pipe not found", "code": "pipe_not_found", "support_ref": "ref-1"}),
    )

    error = cli("pipe", "get", PIPE, "--no-retry").error()

    info = REGISTRY["pipe_not_found"]
    assert error["message"] == "Pipe not found"
    assert error["hint"] == info.hint
    assert error["support_ref"] == "ref-1"
    assert error["recovery"] == {
        "action": info.action,
        "side_effects": info.side_effects,
        "max_retries": info.max_retries,
    }
    context = error["context"]
    assert context["server"] == SERVER
    assert context["profile"] == "test"
    assert context["cli_version"] == CLIENT_VERSION
    assert context["api_version"] == "1.0"
    assert context["request_id"] == server.calls("GET", PIPE_PATH)[0].headers["x-correlation-id"]


def test_extra_error_fields_ride_along_in_details(cli, server, tmp_path):
    server.on(
        "PUT",
        f"/api/cdc/canvases/{PROJECT}/graph",
        answer(409, {"detail": "stale", "code": "project_revision_conflict", "revision": 8}),
    )
    body = tmp_path / "graph.json"
    body.write_text('{"revision": 7, "graph": {"nodes": [], "edges": []}}', encoding="utf-8")

    run = cli("canvas-graph", "put", "--file", str(body), "--yes")

    assert run.code == 1
    error = run.error()
    assert error["code"] == "project_revision_conflict"
    assert error["details"] == {"revision": 8}
    sent = server.calls("PUT", f"/api/cdc/canvases/{PROJECT}/graph")[0]
    assert json.loads(sent.content) == {"revision": 7, "graph": {"nodes": [], "edges": []}}


def test_a_2xx_body_that_does_not_fit_the_model_is_response_invalid_naming_the_field(cli, server):
    server.on("GET", f"{PIPE_PATH}/status", answer(200, {"pipe_uuid": PIPE, "pipe_status": 7, "health": "healthy"}))

    run = cli("pipe", "status", PIPE)

    assert run.code == 8
    error = run.error()
    assert error["code"] == "response_invalid"
    assert error["details"]["field"] == "pipe_status"
    assert "healthy" not in json.dumps(error)


def test_a_network_failure_is_network_error_after_the_retries(cli, server):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server.on("GET", PIPE_PATH, refuse)

    run = cli("pipe", "get", PIPE, "--no-retry")

    assert run.code == 6
    assert run.error()["code"] == "network_error"
    assert run.error()["attempts"] == 1


def test_a_crash_writes_a_trace_file_and_exits_8(cli, server, profile_env):
    def explode(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug in a handler")

    server.on("GET", PIPE_PATH, explode)

    run = cli("pipe", "get", PIPE)

    assert run.code == 8
    error = run.error()
    assert error["code"] == "internal_error"
    log = error["details"]["crash_log"]
    assert log.startswith(str(profile_env["state"]))
    assert "bug in a handler" in Path(log).read_text(encoding="utf-8")


BOARD_PATH = "/api/insights/boards/b1"
FEATURE_OFF = "disabled; feature=custom_dashboards; source=plan"


def _gated_404(header: str | None) -> httpx.Response:
    headers = None if header is None else {"X-Databay-Feature": header}
    return answer(404, {"detail": "Not Found"}, headers=headers)


@pytest.mark.parametrize("source", sorted(FEATURE_SOURCE_HINTS))
def test_a_gated_404_tells_the_cli_which_feature_is_off_and_who_can_change_it(cli, server, source):
    server.on("GET", BOARD_PATH, _gated_404(f"disabled; feature=custom_dashboards; source={source}"))

    run = cli("dashboards", "get", "b1", "--no-retry")

    assert run.code == 1
    assert run.stdout == ""
    error = run.error()
    assert error["code"] == "feature_disabled"
    assert error["status"] == 404
    assert "custom_dashboards" in error["message"]
    assert error["details"] == {"feature": "custom_dashboards", "source": source}
    assert error["hint"] == FEATURE_SOURCE_HINTS[source]
    assert error["recovery"] == {"action": "ask_human", "side_effects": "none", "max_retries": None}


def test_a_gated_404_raises_the_named_exception_from_the_sdk(server):
    server.on("GET", BOARD_PATH, _gated_404("disabled; feature=custom_dashboards; source=ceiling"))

    with (
        server.client() as http,
        Databay(SERVER, StaticToken(TOKEN), project=PROJECT, http=http) as client,
        pytest.raises(DatabayFeatureDisabled) as caught,
    ):
        client.dashboards.get(board_id="b1")

    assert caught.value.code == "feature_disabled"
    assert (caught.value.feature, caught.value.source) == ("custom_dashboards", "ceiling")
    assert caught.value.exit_code == 1
    assert caught.value.hint == FEATURE_SOURCE_HINTS["ceiling"]


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "disabled",
        "enabled; feature=notebooks; source=plan",
        "disabled; feature=notebooks",
        "disabled; feature=; source=plan",
        "disabled; source=plan; feature=notebooks",
        "disabled; feature=notebooks; source=plan; extra=1",
        "disabled;feature=notebooks;source=plan",
        "disabled; feature=notebooks; source=a_source_nobody_documented",
    ],
)
def test_a_404_without_the_documented_header_stays_uncoded(cli, server, header):
    server.on("GET", BOARD_PATH, _gated_404(header))

    run = cli("dashboards", "get", "b1", "--no-retry")

    assert run.code == 8
    error = run.error()
    assert error["code"] == "uncoded_error"
    assert error["message"].endswith("HTTP 404 without a code: Not Found")
    assert error["hint"] == UNCODED_404_HINT


def test_only_a_404_gets_the_hint_about_a_bare_404(cli, server):
    server.on("GET", PIPE_PATH, answer(500, {"detail": "boom"}))

    error = cli("pipe", "get", PIPE, "--no-retry").error()

    assert error["code"] == "uncoded_error"
    assert error["hint"] == LOCAL_CODES["uncoded_error"].hint


def test_a_coded_404_keeps_its_code_even_with_the_header(cli, server):
    response = answer(
        404, {"detail": "Pipe not found", "code": "pipe_not_found"}, headers={"X-Databay-Feature": FEATURE_OFF}
    )
    server.on("GET", PIPE_PATH, response)

    run = cli("pipe", "get", PIPE, "--no-retry")

    assert run.code == 1
    assert run.error()["code"] == "pipe_not_found"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (answer(500, {"detail": "boom"}, headers={"X-Databay-Feature": FEATURE_OFF}), "uncoded_error"),
        (
            httpx.Response(
                404,
                text="<html>Not Found</html>",
                headers={"content-type": "text/html", "X-Databay-Feature": FEATURE_OFF},
            ),
            "non_databay_answer",
        ),
    ],
)
def test_the_header_names_a_feature_only_on_the_json_404_a_gate_answers(cli, server, response, code):
    server.on("GET", PIPE_PATH, response)

    run = cli("pipe", "get", PIPE, "--no-retry")

    assert run.error()["code"] == code
