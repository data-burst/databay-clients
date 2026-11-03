"""Which token a command sends, and how a signed-in profile renews it: once, under a lock, across processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

import pytest
from conftest import (
    CLIENT_CONFIG,
    DISCOVERY,
    DISCOVERY_PATH,
    ISSUER,
    SERVER,
    TOKEN,
    USER_SUB,
    answer,
    write_login_profile,
)

from databay.credentials import Credential, FileStore, credential_key

ME = {"sub": USER_SUB, "preferred_username": "ada", "organization": ["acme"], "principal": "user"}
TOKEN_PATH = "protocol/openid-connect/token"


def _form(request: Any) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _grants(server) -> list[dict[str, str]]:
    return [_form(r) for r in server.calls("POST", "/realms/databay/protocol/openid-connect/token")]


def _token_answer(access: str = "access-new", refresh: str | None = None) -> Any:
    body: dict[str, Any] = {"access_token": access, "expires_in": 300, "token_type": "Bearer"}
    if refresh is not None:
        body["refresh_token"] = refresh
    return answer(200, body)


def _me_for(token: str) -> Any:
    return lambda r: (
        answer(200, ME)
        if r.headers["authorization"] == f"Bearer {token}"
        else answer(401, {"detail": "Token is not active or invalid", "code": "token_invalid"})
    )


def test_a_valid_access_token_is_sent_without_a_refresh(cli, server, login_profile):
    server.on("GET", "/api/auth/me", _me_for(TOKEN))

    run = cli("me", "get")

    assert run.code == 0, run.stderr
    assert _grants(server) == []


def test_a_token_with_less_than_a_minute_left_is_renewed_before_it_is_sent(cli, server, login_profile):
    login_profile.put(access="access-old", expires_in=30)
    server.on("POST", "/realms/databay/protocol/openid-connect/token", _token_answer())
    server.on("GET", "/api/auth/me", _me_for("access-new"))

    run = cli("me", "get")

    assert run.code == 0, run.stderr
    assert _grants(server) == [
        {"grant_type": "refresh_token", "client_id": "databay-cli", "refresh_token": "refresh-1"}
    ]
    stored = login_profile.get()
    assert (stored.access_token, stored.refresh_token) == ("access-new", "refresh-1")
    assert stored.expires_at > time.time() + 250


def test_a_401_refreshes_once_and_sends_again(cli, server, login_profile):
    server.on("POST", "/realms/databay/protocol/openid-connect/token", _token_answer(refresh="refresh-2"))
    server.on("GET", "/api/auth/me", _me_for("access-new"))

    run = cli("me", "get")

    assert run.code == 0, run.stderr
    assert len(_grants(server)) == 1
    assert [r.headers["authorization"] for r in server.calls("GET", "/api/auth/me")] == [
        f"Bearer {TOKEN}",
        "Bearer access-new",
    ]
    assert login_profile.get().refresh_token == "refresh-2"


def test_renewal_posts_to_the_token_endpoint_discovery_names_now(cli, server, login_profile):
    moved = "/realms/databay/protocol/openid-connect/token-moved"
    server.on("GET", DISCOVERY_PATH, answer(200, {**DISCOVERY, "token_endpoint": f"http://keycloak.test{moved}"}))
    server.on("POST", moved, _token_answer())
    server.on("GET", "/api/auth/me", _me_for("access-new"))
    login_profile.put(access="access-old", expires_in=30)

    run = cli("me", "get")

    assert run.code == 0, run.stderr
    assert [_form(r)["grant_type"] for r in server.calls("POST", moved)] == ["refresh_token"]
    assert _grants(server) == []


def test_a_command_that_renews_twice_discovers_once(cli, server, login_profile):
    login_profile.put(access="access-old", expires_in=30)
    server.on("POST", f"/realms/databay/{TOKEN_PATH}", _token_answer("access-refused"), _token_answer("access-new"))
    server.on("GET", "/api/auth/me", _me_for("access-new"))

    run = cli("me", "get")

    assert run.code == 0, run.stderr
    assert len(_grants(server)) == 2
    assert len(server.calls("GET", DISCOVERY_PATH)) == 1


def test_a_credential_saved_with_its_endpoints_renews_at_the_discovered_one_and_is_saved_without_them(
    cli, server, login_profile
):
    path = login_profile.store().path(login_profile.key)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved.update(
        expires_at=time.time() + 30,
        token_endpoint=f"{ISSUER}/protocol/openid-connect/token-gone",
        revocation_endpoint=None,
    )
    path.write_text(json.dumps(saved), encoding="utf-8")
    server.on("POST", f"/realms/databay/{TOKEN_PATH}", _token_answer())
    server.on("GET", "/api/auth/me", _me_for("access-new"))

    run = cli("me", "get")

    assert run.code == 0, run.stderr
    assert len(_grants(server)) == 1
    assert server.calls("POST", "/realms/databay/protocol/openid-connect/token-gone") == []
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["access_token"] == "access-new"
    assert not {"token_endpoint", "revocation_endpoint"} & set(rewritten)


def test_a_credential_with_only_one_of_the_stored_endpoints_is_unreadable(cli, server, login_profile):
    path = login_profile.store().path(login_profile.key)
    saved = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**saved, "token_endpoint": f"{ISSUER}/{TOKEN_PATH}"}), encoding="utf-8")

    run = cli("me", "get")

    assert run.code == 3
    assert run.error()["code"] == "credential_missing"
    assert "unreadable" in run.error()["message"]


def test_a_401_after_the_refresh_is_login_required(cli, server, login_profile):
    server.on("POST", "/realms/databay/protocol/openid-connect/token", _token_answer())
    server.on("GET", "/api/auth/me", answer(401, {"detail": "no", "code": "token_invalid"}))

    run = cli("me", "get")

    assert run.code == 3
    assert run.error()["code"] == "login_required"
    assert run.error()["next"] == f"databay login --server {SERVER} --profile test"
    assert len(_grants(server)) == 1
    assert len(server.calls("GET", "/api/auth/me")) == 2


def test_a_refused_refresh_is_login_required_naming_the_login_command(cli, server, login_profile):
    login_profile.put(access="access-old", expires_in=10)
    server.on(
        "POST",
        "/realms/databay/protocol/openid-connect/token",
        answer(400, {"error": "invalid_grant", "error_description": "Offline session not active"}),
    )

    run = cli("me", "get")

    assert run.code == 3
    error = run.error()
    assert error["code"] == "login_required"
    assert error["next"] == f"databay login --server {SERVER} --profile test"
    assert server.calls("GET", "/api/auth/me") == []


def test_a_profile_whose_credential_is_gone_is_credential_missing(cli, server, login_profile):
    login_profile.store().delete(login_profile.key)

    run = cli("me", "get")

    assert run.code == 3
    assert run.error()["code"] == "credential_missing"
    assert run.error()["next"] == f"databay login --server {SERVER} --profile test"
    assert server.requests == []


def test_a_token_file_others_may_read_is_refused(cli, server, login_profile):
    path = login_profile.store().path(login_profile.key)
    path.chmod(0o644)

    run = cli("me", "get")

    assert run.code == 3
    error = run.error()
    assert error["code"] == "token_file_insecure"
    assert f"chmod 600 {path}" in error["message"]
    assert server.requests == []


def test_databay_token_under_a_login_profile_is_token_source_conflict(cli, server, login_profile, monkeypatch):
    monkeypatch.setenv("DATABAY_TOKEN", "a-token-from-elsewhere")

    run = cli("me", "get")

    assert run.code == 2
    assert run.error()["code"] == "token_source_conflict"
    assert server.requests == []


def test_an_env_profile_sends_databay_token_and_never_reads_a_stored_credential(cli, server, profile_env):
    store = FileStore(profile_env["config"] / "credentials")
    store.put(
        credential_key(SERVER, USER_SUB),
        Credential(
            kind="login",
            server=SERVER,
            sub=USER_SUB,
            issuer="x",
            client_id="databay-cli",
            access_token="stored-token",
            expires_at=time.time() + 3600,
            refresh_token="r",
        ),
    )
    server.on("GET", "/api/auth/me", _me_for(TOKEN))

    run = cli("me", "get")

    assert run.code == 0, run.stderr
    assert {r.headers["authorization"] for r in server.requests} == {f"Bearer {TOKEN}"}


def test_an_api_key_profile_renews_with_the_client_credentials_grant(cli, server, login_profile):
    write_login_profile(login_profile.config, SERVER, login_profile.key, auth="api_key")
    login_profile.put(access="access-old", expires_in=5, kind="api_key")
    server.on("POST", "/realms/databay/protocol/openid-connect/token", _token_answer())
    server.on("GET", "/api/auth/me", _me_for("access-new"))

    run = cli("me", "get")

    assert run.code == 0, run.stderr
    assert _grants(server) == [
        {"grant_type": "client_credentials", "client_id": "apikey-acme-1", "client_secret": "key-secret-value"}
    ]


def test_profile_wins_over_current_profile(cli, server, login_profile):
    config = login_profile.config / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + '\n[profiles.other]\nserver = "http://other.test"\nauth = "env"\n',
        encoding="utf-8",
    )
    server.on("GET", "/api/auth/me", _me_for(TOKEN))

    run = cli("me", "get", "--profile", "test")

    assert run.code == 0, run.stderr
    assert {r.url.host for r in server.requests} == {"databay.test"}


def test_verbose_output_never_carries_a_token(cli, server, login_profile):
    login_profile.put(access="access-old-secret", expires_in=30, refresh="refresh-secret")
    server.on(
        "POST",
        "/realms/databay/protocol/openid-connect/token",
        _token_answer("access-new-secret", "refresh-new-secret"),
    )
    server.on("GET", "/api/auth/me", _me_for("access-new-secret"))

    run = cli("me", "get", "--verbose")

    assert run.code == 0, run.stderr
    assert run.events("request"), run.stderr
    for secret in ("access-old-secret", "refresh-secret", "access-new-secret", "refresh-new-secret"):
        assert secret not in run.stdout + run.stderr


class _Keycloak(BaseHTTPRequestHandler):
    """The Databay API and Keycloak's token endpoint, counting refresh grants, for the parallel test."""

    refreshes = 0
    lock = threading.Lock()

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        issuer = f"http://{self.headers['Host']}/realms/databay"
        if self.path == "/api/auth/client-config":
            self._send(200, CLIENT_CONFIG)
        elif self.path == DISCOVERY_PATH:
            self._send(200, {**DISCOVERY, "issuer": issuer, "token_endpoint": f"{issuer}/{TOKEN_PATH}"})
        elif self.headers["Authorization"] in {"Bearer access-old", "Bearer access-new"}:
            self._send(200, ME)
        else:
            self._send(401, {"detail": "no", "code": "token_invalid"})

    def do_POST(self) -> None:
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        assert self.path == f"/realms/databay/{TOKEN_PATH}"
        assert form["grant_type"] == ["refresh_token"]
        with self.lock:
            type(self).refreshes += 1
        time.sleep(0.3)
        self._send(200, {"access_token": "access-new", "expires_in": 300, "refresh_token": "refresh-1"})

    def log_message(self, *_: Any) -> None:
        return


def test_twenty_parallel_commands_on_a_near_expiry_token_refresh_once(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Keycloak)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    config = tmp_path / "config"
    config.mkdir()
    key = credential_key(base, USER_SUB)
    write_login_profile(config, base, key)
    FileStore(config / "credentials").put(
        key,
        Credential(
            kind="login",
            server=base,
            sub=USER_SUB,
            issuer=f"{base}/realms/databay",
            client_id="databay-cli",
            access_token="access-old",
            expires_at=time.time() + 30,
            refresh_token="refresh-1",
        ),
    )
    env = {
        **{k: v for k, v in os.environ.items() if k != "DATABAY_TOKEN"},
        "DATABAY_CONFIG_DIR": str(config),
        "DATABAY_STATE_DIR": str(tmp_path / "state"),
        "DATABAY_OUTPUT": "json",
    }
    try:
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "databay", "me", "get"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(20)
        ]
        results = [(p.wait(timeout=120), p.stderr.read()) for p in processes]
    finally:
        server.shutdown()

    assert [code for code, _ in results] == [0] * 20, [err for _, err in results]
    assert not [err for _, err in results if "login_required" in err]
    assert _Keycloak.refreshes == 1
    assert FileStore(config / "credentials").get(key).access_token == "access-new"


@pytest.mark.parametrize("auth", ["login", "api_key"])
def test_a_stored_credential_of_the_other_kind_is_credential_missing(cli, server, login_profile, auth):
    write_login_profile(login_profile.config, SERVER, login_profile.key, auth=auth)
    login_profile.put(access=TOKEN, expires_in=3600, kind="api_key" if auth == "login" else "login")

    run = cli("me", "get")

    assert run.code == 3
    assert run.error()["code"] == "credential_missing"
    assert server.requests == []


def test_a_keycloak_outage_while_renewing_is_transient_not_a_sign_in_failure(cli, server, login_profile):
    login_profile.put(access="access-old", expires_in=10)
    server.on(
        "POST",
        "/realms/databay/protocol/openid-connect/token",
        answer(503, {"error": "temporarily_unavailable"}),
    )

    run = cli("me", "get")

    assert run.code == 6
    assert run.error()["code"] == "server_unavailable"


def test_a_stored_login_without_its_refresh_token_is_credential_missing(cli, server, login_profile):
    path = login_profile.store().path(login_profile.key)
    stored = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**stored, "refresh_token": None, "expires_at": 0}), encoding="utf-8")

    run = cli("me", "get")

    assert run.code == 3
    assert run.error()["code"] == "credential_missing"
    assert server.requests == []


def test_an_unknown_token_store_is_profile_invalid_even_without_a_credential(cli, profile_env):
    (profile_env["config"] / "config.toml").write_text(
        f'current_profile = "test"\n[profiles.test]\nserver = "{SERVER}"\nauth = "login"\ntoken_store = "vault"\n',
        encoding="utf-8",
    )

    run = cli("profile", "list")

    assert run.code == 3
    assert run.error()["code"] == "profile_invalid"
