"""``databay source create <type>`` and its SDK method: flags nest under ``config``, and a secret is never a flag."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from conftest import PROJECT, SERVER, TOKEN, answer

from databay import Databay
from databay.errors import DatabayUsageError
from databay.models.source import PostgresConnectedConfig
from databay.operations.source import SOURCE_CREATE
from databay.tokens import StaticToken

SOURCE = "/api/configstore/source"
CREATED = {
    "uuid": "s1",
    "name": "shop",
    "source_type": "postgres",
    "auth_kind": "static_credentials",
    "status": "connected",
    "pipe_eligibility": {"allowed": True, "reason": None},
}
DATABASE_FLAGS = ("--host", "db.example.com", "--port", "5432", "--db-name", "shop", "--username", "databay")
DATABASE_CONFIG = {"host": "db.example.com", "port": 5432, "db_name": "shop", "username": "databay"}


@dataclass(frozen=True)
class Variant:
    """How one source type's command is called, what it sends, and which of its fields are credentials."""

    secrets: tuple[str, ...]
    flags: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)


VARIANTS = {
    "postgres": Variant(("password",), DATABASE_FLAGS, DATABASE_CONFIG),
    "mysql": Variant(("password",), DATABASE_FLAGS, DATABASE_CONFIG),
    "mssql": Variant(("password",), DATABASE_FLAGS, DATABASE_CONFIG),
    "mongodb": Variant(("password",), DATABASE_FLAGS, DATABASE_CONFIG),
    "file_upload": Variant(()),
    "shopify": Variant(("access_token",), ("--start-date", "2024-01-01"), {"start_date": "2024-01-01"}),
    "meta_ads": Variant(
        ("access_token", "app_secret"),
        ("--account-id", "act_1", "--app-id", "app", "--extra-account-ids", "act_2"),
        {"account_id": "act_1", "app_id": "app", "extra_account_ids": ["act_2"]},
    ),
    "google_ads": Variant(
        ("client_secret", "developer_token", "refresh_token"),
        ("--customer-id", "123", "--client-id", "cid"),
        {"customer_id": "123", "client_id": "cid"},
    ),
    "ga4": Variant(
        ("refresh_token", "service_account_json"),
        ("--property-id", "9", "--multi-property"),
        {"property_id": "9", "multi_property": True},
    ),
    "instagram": Variant(("access_token",), ("--login", "instagram"), {"login": "instagram"}),
    "linkedin_pages": Variant(("access_token",), ("--organization-id", "7"), {"organization_id": "7"}),
}
EACH_VARIANT = sorted(VARIANTS)
EACH_SECRET = [(variant, secret) for variant in EACH_VARIANT for secret in VARIANTS[variant].secrets]


def _flag(secret: str) -> str:
    return secret.replace("_", "-")


def test_every_source_type_is_covered_here():
    assert {key for key, _, _ in SOURCE_CREATE.body.variants} == set(VARIANTS)


@pytest.mark.parametrize("variant", EACH_VARIANT)
def test_a_variants_secret_fields_are_exactly_its_credentials(variant):
    _, fields = SOURCE_CREATE.body.variant(variant)

    assert sorted(param.name for param in fields if param.secret) == sorted(VARIANTS[variant].secrets)


@pytest.mark.parametrize("variant", EACH_VARIANT)
def test_flags_nest_under_config_with_the_type_and_the_body_fields_stay_beside_it(cli, server, monkeypatch, variant):
    server.on("POST", SOURCE, answer(200, CREATED))
    spec = VARIANTS[variant]
    secret_flags: list[str] = []
    secret_values = {}
    for secret in spec.secrets:
        monkeypatch.setenv(f"SECRET_{secret.upper()}", f"value-of-{secret}")
        secret_flags += [f"--{_flag(secret)}-env", f"SECRET_{secret.upper()}"]
        secret_values[secret] = f"value-of-{secret}"

    run = cli("source", "create", variant, "--name", "shop", "--allow-incomplete", *spec.flags, *secret_flags)

    assert run.code == 0, run.stderr
    sent = server.calls("POST", SOURCE)[0]
    assert json.loads(sent.content) == {
        "name": "shop",
        "allow_incomplete": True,
        "config": {"source_type": variant, **spec.config, **secret_values},
    }
    assert sent.headers["x-databay-project-id"] == PROJECT
    assert sent.headers["idempotency-key"] == run.json()["idempotency_key"]


@pytest.mark.parametrize(("variant", "secret"), EACH_SECRET)
def test_a_secret_is_refused_as_a_plain_flag_and_never_sent(cli, server, variant, secret):
    run = cli("source", "create", variant, "--name", "shop", f"--{_flag(secret)}", "in-shell-history")

    assert run.code == 2
    assert run.error()["code"] == "usage_invalid"
    assert f"--{_flag(secret)}" in run.error()["message"]
    assert server.requests == []


@pytest.mark.parametrize(("variant", "secret"), EACH_SECRET)
def test_help_offers_a_secret_only_from_the_environment_or_stdin(cli, variant, secret):
    run = cli("source", "create", variant, "--help")

    assert f"--{_flag(secret)}-env NAME" in run.stdout
    assert f"--{_flag(secret)}-stdin" in run.stdout
    assert f"--{_flag(secret)} " not in run.stdout


def test_a_secret_from_stdin_nests_under_config(cli, server, monkeypatch):
    server.on("POST", SOURCE, answer(200, CREATED))

    run = cli(
        "source", "create", "shopify", "--name", "shop", "--access-token-stdin", stdin="tok\n", monkeypatch=monkeypatch
    )

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("POST", SOURCE)[0].content)["config"]["access_token"] == "tok"


def test_a_required_secret_with_no_source_is_secret_required(cli, server, monkeypatch):
    run = cli("source", "create", "shopify", "--name", "shop", stdin="", monkeypatch=monkeypatch)

    assert run.code == 2
    assert run.error()["code"] == "secret_required"
    assert server.requests == []


def test_verbose_output_redacts_a_secret_nested_under_config(cli, server, monkeypatch):
    server.on("POST", SOURCE, answer(200, CREATED))
    monkeypatch.setenv("PGPASSWORD", "very-secret-value")

    run = cli(
        "source", "create", "postgres", "--name", "shop", *DATABASE_FLAGS, "--password-env", "PGPASSWORD", "--verbose"
    )

    assert run.code == 0, run.stderr
    assert "very-secret-value" not in run.stderr + run.stdout
    logged = next(e for e in run.events("request") if e["url"].endswith(SOURCE))
    assert logged["body"]["config"]["password"] == "<redacted>"


def _body_file(tmp_path, body: Any) -> str:
    path = tmp_path / "source.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def test_a_body_file_holds_the_nested_body_and_the_command_names_its_type(cli, server, tmp_path):
    server.on("POST", SOURCE, answer(200, CREATED))
    config = {**DATABASE_CONFIG, "password": "from-the-file"}

    run = cli("source", "create", "postgres", "--file", _body_file(tmp_path, {"name": "shop", "config": config}))

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("POST", SOURCE)[0].content) == {
        "name": "shop",
        "config": {"source_type": "postgres", **config},
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param({"name": "s", "config": {"source_type": "mysql"}}, "source_type='mysql'", id="another type"),
        pytest.param({"name": "s", "config": "postgres"}, "config must hold a JSON object", id="config not an object"),
        pytest.param(["s"], "must hold a JSON object", id="body not an object"),
    ],
)
def test_a_body_file_that_does_not_fit_the_command_is_refused(cli, server, tmp_path, body, message):
    run = cli("source", "create", "postgres", "--file", _body_file(tmp_path, body))

    assert run.code == 2
    assert message in run.error()["message"]
    assert server.requests == []


def test_a_variants_flag_and_a_body_file_are_not_both_taken(cli, server, tmp_path):
    run = cli(
        "source", "create", "postgres", "--host", "h", "--file", _body_file(tmp_path, {"name": "s", "config": {}})
    )

    assert run.code == 2
    assert "--host" in run.error()["message"]
    assert server.requests == []


def test_a_top_level_discriminated_body_stays_flat(cli, server):
    server.on("POST", "/api/configstore/source/precheck", answer(200, {"ready": True, "checks": []}))

    cli("source", "precheck", "mongodb", *DATABASE_FLAGS)

    sent = json.loads(server.calls("POST", "/api/configstore/source/precheck")[0].content)
    assert sent == {"source_type": "mongodb", **DATABASE_CONFIG}


def _client(server) -> Databay:
    return Databay(SERVER, StaticToken(TOKEN), project=PROJECT, http=server.client())


def test_the_sdk_sends_a_config_model_nested_with_its_type_and_a_key(server, profile_env):
    server.on("POST", SOURCE, answer(200, CREATED))

    with _client(server) as client:
        created = client.source.create(
            name="shop",
            config=PostgresConnectedConfig(host="h", port=5432, db_name="d", username="u", password="pw"),
            allow_incomplete=True,
        )

    assert created.uuid == "s1"
    sent = server.calls("POST", SOURCE)[0]
    assert json.loads(sent.content) == {
        "name": "shop",
        "allow_incomplete": True,
        "config": {
            "source_type": "postgres",
            "host": "h",
            "port": 5432,
            "db_name": "d",
            "username": "u",
            "password": "pw",
        },
    }
    assert sent.headers["idempotency-key"]


def test_the_sdk_refuses_a_config_with_no_known_type(server, profile_env):
    with _client(server) as client, pytest.raises(DatabayUsageError, match="config whose source_type"):
        client.source.create(name="shop", config={"source_type": "oracle"})

    assert server.calls("POST", SOURCE) == []
