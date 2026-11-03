"""
``databay login`` and ``databay logout`` against a scripted server and a scripted Keycloak.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import stat
import threading
import tomllib
import webbrowser
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import DISCOVERY, DISCOVERY_PATH, ISSUER, SERVER, USER_SUB, answer, jwt
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from databay.credentials import FileStore, credential_key

DEVICE = "/realms/databay/protocol/openid-connect/auth/device"
TOKEN_PATH = "/realms/databay/protocol/openid-connect/token"
REVOKE_PATH = "/realms/databay/protocol/openid-connect/revoke"
DEVICE_ANSWER = {
    "device_code": "device-1",
    "user_code": "ABCD-EFGH",
    "verification_uri": f"{ISSUER}/device",
    "verification_uri_complete": f"{ISSUER}/device?user_code=ABCD-EFGH",
    "expires_in": 60,
    "interval": 0,
}


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _tokens(refresh: str = "refresh-2", scope: str = "openid offline_access", sub: str = USER_SUB) -> httpx.Response:
    return answer(
        200,
        {
            "access_token": jwt(sub, preferred_username="ada"),
            "expires_in": 300,
            "refresh_token": refresh,
            "scope": scope,
        },
    )


@pytest.fixture
def keycloak(server, monkeypatch):
    """Discovery and the device flow; the token endpoint answers pending once, then tokens."""
    for name in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    server.on("GET", DISCOVERY_PATH, answer(200, DISCOVERY))
    server.on("POST", DEVICE, answer(200, DEVICE_ANSWER))
    server.on("POST", TOKEN_PATH, answer(400, {"error": "authorization_pending"}), _tokens())
    server.on("POST", REVOKE_PATH, answer(200))
    return server


def _config(profile_env) -> dict[str, Any]:
    return tomllib.loads((profile_env["config"] / "config.toml").read_text(encoding="utf-8"))


def test_a_device_login_saves_the_profile_and_a_0600_credential(cli, keycloak, profile_env):
    run = cli("login", "--server", SERVER, "--device", "--token-store", "file", "--profile", "work")

    assert run.code == 0, run.stderr
    key = credential_key(SERVER, USER_SUB)
    profile = _config(profile_env)["profiles"]["work"]
    assert profile == {"server": SERVER, "auth": "login", "credential": key, "token_store": "file"}
    store = FileStore(profile_env["config"] / "credentials")
    assert stat.S_IMODE(store.path(key).stat().st_mode) == 0o600
    assert store.get(key).refresh_token == "refresh-2"
    assert run.json()["result"]["current"] is False
    assert run.json()["next"] == "databay profile use work"
    assert run.events("device_login")[0]["user_code"] == "ABCD-EFGH"


def test_the_device_request_and_the_token_request_carry_pkce_and_offline_access(cli, keycloak):
    cli("login", "--server", SERVER, "--device", "--token-store", "file")

    started = _form(keycloak.calls("POST", DEVICE)[0])
    redeemed = _form(keycloak.calls("POST", TOKEN_PATH)[-1])
    assert started["scope"] == "openid offline_access"
    assert started["code_challenge_method"] == "S256"
    digest = hashlib.sha256(redeemed["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == started["code_challenge"]
    assert redeemed["device_code"] == "device-1"


def test_a_first_login_becomes_the_current_profile(cli, keycloak, profile_env):
    (profile_env["config"] / "config.toml").unlink()

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file")

    assert run.code == 0, run.stderr
    assert _config(profile_env)["current_profile"] == "default"
    assert run.json()["next"] == "databay status"


@pytest.mark.parametrize(
    "environ",
    [{"SSH_CONNECTION": "10.0.0.1 22 10.0.0.2 22"}, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}],
    ids=["ssh", "no-display"],
)
def test_without_a_reachable_browser_the_device_flow_is_chosen_and_said(cli, keycloak, monkeypatch, environ):
    monkeypatch.setattr("sys.platform", "linux")
    for name, value in environ.items():
        monkeypatch.setenv(name, value)

    run = cli("login", "--server", SERVER, "--token-store", "file")

    assert run.code == 0, run.stderr
    assert run.events("device_login_chosen")
    assert keycloak.calls("POST", DEVICE)


def test_a_browser_login_returns_to_a_loopback_port_and_redeems_with_pkce(cli, keycloak, monkeypatch, profile_env):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "get", object)
    keycloak.on("POST", TOKEN_PATH, _tokens())
    opened: list[str] = []

    def browser(url: str) -> bool:
        opened.append(url)
        query = parse_qs(urlparse(url).query)
        redirect = query["redirect_uri"][0]
        callback = f"{redirect}?code=code-1&state={query['state'][0]}"
        threading.Thread(target=httpx.get, args=(callback,), daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", browser)

    run = cli("login", "--server", SERVER, "--token-store", "file")

    assert run.code == 0, run.stderr
    query = parse_qs(urlparse(opened[0]).query)
    redirect = urlparse(query["redirect_uri"][0])
    assert (redirect.scheme, redirect.hostname, redirect.path) == ("http", "127.0.0.1", "/callback")
    assert query["scope"] == ["openid offline_access"]
    redeemed = _form(keycloak.calls("POST", TOKEN_PATH)[0])
    assert redeemed["code"] == "code-1"
    assert redeemed["redirect_uri"] == query["redirect_uri"][0]
    digest = hashlib.sha256(redeemed["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"][0]
    assert keycloak.calls("POST", DEVICE) == []
    assert "default" in _config(profile_env)["profiles"]


def test_a_browser_return_with_another_state_stores_nothing(cli, keycloak, monkeypatch, profile_env):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "get", object)

    def browser(url: str) -> bool:
        redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]
        threading.Thread(target=httpx.get, args=(f"{redirect}?code=code-1&state=forged",), daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", browser)

    run = cli("login", "--server", SERVER, "--token-store", "file")

    assert run.code == 3
    assert run.error()["code"] == "login_failed"
    assert keycloak.calls("POST", TOKEN_PATH) == []
    assert "default" not in _config(profile_env)["profiles"]


def test_an_idle_browser_connection_does_not_hold_up_the_callback(cli, keycloak, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "get", object)
    keycloak.on("POST", TOKEN_PATH, _tokens())
    idle: list[socket.socket] = []

    def browser(url: str) -> bool:
        query = parse_qs(urlparse(url).query)
        redirect = urlparse(query["redirect_uri"][0])
        idle.append(socket.create_connection((redirect.hostname, redirect.port)))
        callback = f"{query['redirect_uri'][0]}?code=code-1&state={query['state'][0]}"
        threading.Thread(target=httpx.get, args=(callback,), kwargs={"timeout": 30}, daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", browser)

    run = cli("login", "--server", SERVER, "--token-store", "file")

    idle[0].close()
    assert run.code == 0, run.stderr


def test_a_browser_return_without_a_code_is_login_failed(cli, keycloak, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "get", object)

    def browser(url: str) -> bool:
        query = parse_qs(urlparse(url).query)
        callback = f"{query['redirect_uri'][0]}?state={query['state'][0]}"
        threading.Thread(target=httpx.get, args=(callback,), daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", browser)

    run = cli("login", "--server", SERVER, "--token-store", "file")

    assert run.code == 3
    assert run.error()["code"] == "login_failed"
    assert keycloak.calls("POST", TOKEN_PATH) == []


def test_a_login_without_offline_access_is_refused_and_stores_nothing(cli, keycloak, profile_env):
    keycloak.on("POST", TOKEN_PATH, _tokens(scope="openid profile"))

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file", "--profile", "work")

    assert run.code == 3
    assert run.error()["code"] == "offline_access_not_granted"
    assert "work" not in _config(profile_env)["profiles"]
    assert not (profile_env["config"] / "credentials").exists()


@pytest.mark.parametrize(("error", "code"), [("access_denied", "access_denied"), ("expired_token", "expired_token")])
def test_a_refused_or_expired_device_login_exits_3(cli, keycloak, error, code):
    keycloak.on("POST", TOKEN_PATH, answer(400, {"error": error}))

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file")

    assert run.code == 3
    assert run.error()["code"] == code


def test_a_shopify_server_is_unsupported_product_before_any_sign_in(cli, keycloak):
    keycloak.client_config["product"] = "shopify"

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file")

    assert run.code == 7
    assert run.error()["code"] == "unsupported_product"
    assert [r.url.path for r in keycloak.requests] == ["/api/auth/client-config"]


def test_without_a_keyring_login_names_the_file_store_before_any_request(cli, keycloak):
    import keyring
    from keyring.backends import fail

    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        run = cli("login", "--server", SERVER, "--device")
    finally:
        keyring.set_keyring(previous)

    assert run.code == 3
    error = run.error()
    assert error["code"] == "keyring_unavailable"
    assert "--token-store file" in error["message"]
    assert keycloak.requests == []


def test_signing_in_again_revokes_the_refresh_token_it_replaces(cli, keycloak, login_profile):
    run = cli("login", "--server", SERVER, "--device", "--token-store", "file", "--profile", "test")

    assert run.code == 0, run.stderr
    assert [_form(r)["token"] for r in keycloak.calls("POST", REVOKE_PATH)] == ["refresh-1"]
    assert login_profile.get().refresh_token == "refresh-2"
    assert run.json()["result"]["revoked_superseded"] == 1


def test_another_user_on_the_profile_revokes_and_deletes_the_credential_it_replaces(cli, keycloak, login_profile):
    keycloak.on("POST", TOKEN_PATH, _tokens(sub="7a2b0000-0000-4000-8000-0000000000b0b"))

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file", "--profile", "test")

    assert run.code == 0, run.stderr
    assert [_form(r)["token"] for r in keycloak.calls("POST", REVOKE_PATH)] == ["refresh-1"]
    assert login_profile.get() is None


def test_an_api_key_signs_in_from_a_variable_and_never_prints_its_secret(cli, keycloak, profile_env, monkeypatch):
    monkeypatch.setenv("CI_KEY", "key-secret-value")
    keycloak.on("POST", TOKEN_PATH, answer(200, {"access_token": jwt("svc-sub"), "expires_in": 300}))

    run = cli(
        "login", "--server", SERVER, "--profile", "ci", "--api-key-id", "apikey-acme-1",
        "--api-key-env", "CI_KEY", "--token-store", "file", "--verbose",
    )  # fmt: skip

    assert run.code == 0, run.stderr
    assert _form(keycloak.calls("POST", TOKEN_PATH)[0]) == {
        "grant_type": "client_credentials",
        "client_id": "apikey-acme-1",
        "client_secret": "key-secret-value",
    }
    assert _config(profile_env)["profiles"]["ci"]["auth"] == "api_key"
    stored = FileStore(profile_env["config"] / "credentials").get(credential_key(SERVER, "svc-sub"))
    assert (stored.kind, stored.client_secret, stored.refresh_token) == ("api_key", "key-secret-value", None)
    assert "key-secret-value" not in run.stdout + run.stderr


def test_an_api_key_secret_from_stdin(cli, keycloak, monkeypatch):
    keycloak.on("POST", TOKEN_PATH, answer(200, {"access_token": jwt("svc-sub"), "expires_in": 300}))

    run = cli(
        "login", "--server", SERVER, "--api-key-id", "apikey-acme-1", "--api-key-stdin", "--token-store", "file",
        stdin="key-from-stdin\n", monkeypatch=monkeypatch,
    )  # fmt: skip

    assert run.code == 0, run.stderr
    assert _form(keycloak.calls("POST", TOKEN_PATH)[0])["client_secret"] == "key-from-stdin"


def test_an_api_key_without_a_secret_source_is_secret_required(cli, keycloak):
    run = cli("login", "--server", SERVER, "--api-key-id", "apikey-acme-1", "--token-store", "file")

    assert run.code == 2
    assert run.error()["code"] == "secret_required"
    assert keycloak.requests == []


def test_logout_revokes_and_clears_the_credential_from_every_profile(cli, keycloak, login_profile, profile_env):
    config = profile_env["config"] / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f'\n[profiles.twin]\nserver = "{SERVER}"\nauth = "login"\ncredential = "{login_profile.key}"\n'
        'token_store = "file"\n',
        encoding="utf-8",
    )

    run = cli("logout")

    assert run.code == 0, run.stderr
    assert [_form(r) for r in keycloak.calls("POST", REVOKE_PATH)] == [
        {"client_id": "databay-cli", "token": "refresh-1", "token_type_hint": "refresh_token"}
    ]
    assert run.json()["result"] == {"credential": login_profile.key, "profiles": ["test", "twin"], "revoked": True}
    assert login_profile.get() is None
    profiles = _config(profile_env)["profiles"]
    assert "credential" not in profiles["test"]
    assert "credential" not in profiles["twin"]
    after = cli("me", "get")
    assert after.code == 3
    assert after.error()["code"] == "credential_missing"


def test_a_failed_revocation_exits_1_and_still_signs_out_locally(cli, keycloak, login_profile, profile_env):
    keycloak.on("POST", REVOKE_PATH, answer(503))

    run = cli("logout")

    assert run.code == 1
    error = run.error()
    assert error["code"] == "revoke_failed"
    assert error["recovery"]["action"] == "ask_human"
    assert login_profile.get() is None
    assert "credential" not in _config(profile_env)["profiles"]["test"]


def test_logout_of_a_credential_saved_with_its_endpoints_revokes_at_the_discovered_one(cli, keycloak, login_profile):
    path = login_profile.store().path(login_profile.key)
    saved = json.loads(path.read_text(encoding="utf-8"))
    gone = "/realms/databay/protocol/openid-connect/revoke-gone"
    saved.update(
        token_endpoint=f"{ISSUER}/protocol/openid-connect/token", revocation_endpoint=f"http://keycloak.test{gone}"
    )
    path.write_text(json.dumps(saved), encoding="utf-8")

    run = cli("logout")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["revoked"] is True
    assert [_form(r)["token"] for r in keycloak.calls("POST", REVOKE_PATH)] == ["refresh-1"]
    assert keycloak.calls("POST", gone) == []


class MemoryKeyring(KeyringBackend):
    """An OS keyring held in memory: keyring's own backend interface, without the OS."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.entries.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


@pytest.fixture
def memory_keyring():
    import keyring

    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_a_keyring_login_then_logout_leaves_the_keyring_empty(cli, keycloak, memory_keyring):
    login = cli("login", "--server", SERVER, "--device", "--profile", "work")
    stored = dict(memory_keyring.entries)

    logout = cli("logout", "--profile", "work")

    assert login.code == 0, login.stderr
    assert list(stored) == [("databay-cli", credential_key(SERVER, USER_SUB))]
    assert logout.code == 0, logout.stderr
    assert memory_keyring.entries == {}


def test_logout_of_a_keyring_entry_already_gone_still_signs_the_profile_out(cli, keycloak, memory_keyring, profile_env):
    login = cli("login", "--server", SERVER, "--device", "--profile", "work")
    memory_keyring.entries.clear()

    run = cli("logout", "--profile", "work")

    assert login.code == 0, login.stderr
    assert run.code == 0, run.stderr
    assert run.json()["result"]["revoked"] is False
    assert "credential" not in _config(profile_env)["profiles"]["work"]
