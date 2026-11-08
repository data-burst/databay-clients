"""The ``dashboards`` commands: what each one sends, how a card's spec gets in, and what the answer parses into."""

from __future__ import annotations

import json

import pytest
from conftest import PROJECT, answer
from pydantic import ValidationError

from databay.models.shared import CardParam, CardQuery

BOARD_ID = "b1"
BOARD_PATH = f"/api/insights/boards/{BOARD_ID}"
SPEC = {"spec_version": 1, "query": {"sql": "SELECT 1 AS n"}, "chart": {"type": "kpi"}}
CARD_ID = "c1"
CARD = {
    "id": CARD_ID,
    "project_id": PROJECT,
    "warehouse_id": "w1",
    "title": "N",
    "spec": SPEC,
    "revision": 1,
    "boards": [{"id": BOARD_ID, "name": "Ads"}],
    "created_by": "u",
    "updated_by": "u",
    "updated_at": "2026-10-31T10:00:00Z",
}
BOARD = {
    "id": BOARD_ID,
    "project_id": PROJECT,
    "folder_id": None,
    "name": "Ads",
    "description": None,
    "revision": 2,
    "refresh_interval_s": None,
    "source_builtin": None,
    "updated_by": "u",
    "updated_at": "2026-10-31T10:00:00Z",
    "created_by": "u",
    "created_at": "2026-10-31T10:00:00Z",
    "filters": [
        {"key": "range", "type": "date_range", "label": "Range", "default": "last_30_days"},
        {"key": "channel", "type": "select", "label": "Channel"},
    ],
    "placements": [{"card_id": CARD_ID, "x": 0, "y": 0, "w": 6, "h": 3}],
    "cards": [CARD],
}
CARDS_PATH = f"/api/insights/projects/{PROJECT}/cards"


CARD_BODY = {"title": "N", "warehouse_id": "w1", "spec": SPEC}


def _card_file(tmp_path) -> str:
    path = tmp_path / "card.json"
    path.write_text(json.dumps(CARD_BODY), encoding="utf-8")
    return str(path)


def test_list_reads_the_active_projects_tree(cli, server):
    tree = {"project_id": PROJECT, "folders": [], "boards": [], "card_count": 0}
    server.on("GET", f"/api/insights/projects/{PROJECT}/folders", answer(200, tree))

    run = cli("dashboards", "list")

    assert run.code == 0, run.stderr
    assert run.json()["result"] == tree


def test_create_sends_the_project_in_the_path_and_a_new_key(cli, server):
    path = "/api/insights/projects/proj-other/boards"
    server.on("POST", path, answer(201, BOARD))

    run = cli("dashboards", "create", "--name", "Ads", "--project", "proj-other")

    assert run.code == 0, run.stderr
    sent = server.calls("POST", path)[0]
    assert json.loads(sent.content) == {"name": "Ads"}
    assert sent.headers["idempotency-key"] == run.json()["idempotency_key"]


def test_a_board_answer_parses_with_a_spec_left_sparse(cli, server):
    server.on("GET", BOARD_PATH, answer(200, BOARD))

    run = cli("dashboards", "get", BOARD_ID)

    assert run.code == 0, run.stderr
    assert run.json()["result"]["cards"][0]["spec"] == SPEC


def test_a_fixed_parameter_and_a_pinned_dataset_parse_into_their_types():
    query = CardQuery.model_validate(
        {"sql": "SELECT 1", "dataset_versions": {"ads": 2}, "params": {"n": {"type": "Int64", "value": 1}}}
    )

    assert query.dataset_versions == {"ads": 2}
    assert isinstance(query.params["n"], CardParam)
    with pytest.raises(ValidationError):
        CardQuery.model_validate({"sql": "SELECT 1", "dataset_versions": {"ads": "latest"}})


def test_a_card_spec_comes_from_a_body_file(cli, server, tmp_path):
    server.on("POST", CARDS_PATH, answer(201, CARD))

    run = cli("dashboards-card", "create", "--file", _card_file(tmp_path))

    assert run.code == 0, run.stderr
    sent = json.loads(server.calls("POST", CARDS_PATH)[0].content)
    assert sent == CARD_BODY


def test_a_card_spec_comes_from_stdin(cli, server, monkeypatch):
    server.on("POST", CARDS_PATH, answer(201, CARD))
    body = json.dumps(CARD_BODY)

    run = cli("dashboards-card", "create", "--file", "-", stdin=body, monkeypatch=monkeypatch)

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("POST", CARDS_PATH)[0].content)["spec"] == SPEC


def test_the_spec_flag_takes_json_and_refuses_anything_else(cli, server):
    server.on("POST", CARDS_PATH, answer(201, CARD))

    good = cli("dashboards-card", "create", "--warehouse-id", "w1", "--title", "N", "--spec", json.dumps(SPEC))
    bad = cli("dashboards-card", "create", "--warehouse-id", "w1", "--title", "N", "--spec", "not json")

    assert good.code == 0, good.stderr
    assert json.loads(server.calls("POST", CARDS_PATH)[0].content)["spec"] == SPEC
    assert bad.code == 2
    assert "--spec takes JSON" in bad.error()["message"]
    assert len(server.calls("POST", CARDS_PATH)) == 1


def test_a_card_create_needs_its_spec(cli, server):
    run = cli("dashboards-card", "create", "--warehouse-id", "w1", "--title", "N")

    assert run.code == 2
    assert "--spec" in run.error()["message"]
    assert server.calls("POST", CARDS_PATH) == []


def test_each_delete_and_the_whole_board_save_ask_for_yes(cli, server, tmp_path):
    for words in (
        ("dashboards", "delete", BOARD_ID, "--revision", "1"),
        ("dashboards-card", "delete", CARD_ID, "--revision", "1"),
        ("dashboards-folder", "delete", "f1"),
        ("dashboards", "replace", BOARD_ID, "--file", _card_file(tmp_path)),
    ):
        run = cli(*words)

        assert run.code == 2, words
        assert run.error()["code"] == "confirmation_required", words
    assert [r.url.path for r in server.requests if r.url.path.startswith("/api/insights")] == []


def test_a_delete_with_yes_sends_the_revision_it_read(cli, server):
    server.on("DELETE", BOARD_PATH, answer(204))

    run = cli("dashboards", "delete", BOARD_ID, "--revision", "3", "--yes")

    assert run.code == 0, run.stderr
    assert server.calls("DELETE", BOARD_PATH)[0].url.params["revision"] == "3"


def test_update_sends_the_refresh_interval_as_a_json_number(cli, server):
    server.on("PATCH", BOARD_PATH, answer(200, BOARD))

    run = cli("dashboards", "update", BOARD_ID, "--revision", "2", "--refresh-interval-s", "60")

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("PATCH", BOARD_PATH)[0].content) == {"revision": 2, "refresh_interval_s": 60}


def test_a_run_is_not_a_create_and_sends_no_key(cli, server):
    outcome = {"revision": 1, "cards": []}
    server.on("POST", f"{BOARD_PATH}/run", answer(200, outcome))

    run = cli("dashboards", "run", BOARD_ID, "--filters", '{"range": {"start": "2026-01-01"}}')

    assert run.code == 0, run.stderr
    sent = server.calls("POST", f"{BOARD_PATH}/run")[0]
    assert json.loads(sent.content) == {"filters": {"range": {"start": "2026-01-01"}}}
    assert "idempotency-key" not in sent.headers


def test_a_reused_key_names_its_error(cli, server, tmp_path):
    reused = {
        "detail": "This Idempotency-Key was already used for a different request.",
        "code": "idempotency_key_reused",
    }
    server.on("POST", CARDS_PATH, answer(409, reused))

    run = cli("dashboards-card", "create", "--file", _card_file(tmp_path), "--idempotency-key", "k1")

    assert run.code != 0
    assert run.error()["code"] == "idempotency_key_reused"


def test_the_dashboards_gate_answering_unknown_entitlements_is_its_named_error(cli, server):
    path = f"/api/insights/projects/{PROJECT}/folders"
    server.on("GET", path, answer(503, {"detail": "Entitlements are unavailable.", "code": "entitlements_unavailable"}))

    run = cli("dashboards", "list", "--no-retry")

    assert run.error()["code"] == "entitlements_unavailable"


def test_the_help_names_each_dashboards_command_for_what_it_does(cli):
    run = cli("dashboards", "--help")

    assert run.code == 0
    assert "List the project's folders and dashboards" in run.stdout
    assert "Create a dashboard" in run.stdout
    assert "Folder Tree" not in run.stdout
