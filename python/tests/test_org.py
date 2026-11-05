"""The active organization: ``databay org list``, ``databay org switch``, ``login --org`` and ``status``."""

from __future__ import annotations

import csv
import io
import json
import tomllib
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import DISCOVERY, DISCOVERY_PATH, PROJECT, SERVER, TOKEN, USER_SUB, answer, jwt

ORGS_PATH = "/api/auth/me/organizations"
ACTIVE_PATH = "/api/auth/me/active-org"
ME_PATH = "/api/auth/me"
TOKEN_PATH = "/realms/databay/protocol/openid-connect/token"
DEVICE_PATH = "/realms/databay/protocol/openid-connect/auth/device"
RENEWED = "access-renewed"


def _org(key: str, name: str, *, active: bool, role: str | None = "editor", enabled: bool = True) -> dict[str, Any]:
    return {"organization": key, "display_name": name, "role": role, "is_active": active, "enabled": enabled}


def _listing(*, active: str | None, disabled: frozenset[str] = frozenset()) -> dict[str, Any]:
    orgs = [
        _org("org-acme", "Acme", active=active == "org-acme", role="org_admin", enabled="org-acme" not in disabled),
        _org("org-beta", "Beta Labs", active=active == "org-beta", enabled="org-beta" not in disabled),
    ]
    usable = sum(org["enabled"] for org in orgs)
    return {
        "organizations": orgs,
        "active_org": active,
        "needs_org_choice": active is None and usable > 0,
        "usable_orgs": usable,
    }


def _me_naming(org_by_token: dict[str, str | None]) -> Any:
    """``/auth/me`` as the server reads each token: the organization it names."""

    def handle(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        org = org_by_token[token]
        return answer(200, {"sub": USER_SUB, "organization": [org] if org else [], "principal": "user"})

    return handle


def _renewal(access: str = RENEWED) -> httpx.Response:
    return answer(200, {"access_token": access, "expires_in": 300, "refresh_token": "refresh-2"})


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _config(config) -> dict[str, Any]:
    return tomllib.loads((config / "config.toml").read_text(encoding="utf-8"))


def test_org_list_prints_each_org_and_which_one_is_active(cli, server):
    server.on("GET", ORGS_PATH, answer(200, _listing(active="org-beta")))

    run = cli("org", "list", "--output", "csv")

    assert run.code == 0, run.stderr
    rows = list(csv.DictReader(io.StringIO(run.stdout)))
    assert [(r["organization"], r["is_active"], r["enabled"]) for r in rows] == [
        ("org-acme", "false", "true"),
        ("org-beta", "true", "true"),
    ]


def test_org_switch_by_name_stores_it_renews_the_token_and_clears_the_project(cli, server, login_profile):
    server.on("GET", ORGS_PATH, answer(200, _listing(active="org-acme")))
    server.on("PUT", ACTIVE_PATH, answer(200, {"active_org": "org-beta"}))
    server.on("POST", TOKEN_PATH, _renewal())
    server.on("GET", ME_PATH, _me_naming({TOKEN: "org-acme", RENEWED: "org-beta"}))

    run = cli("org", "switch", "beta labs")

    assert run.code == 0, run.stderr
    assert json.loads(server.calls("PUT", ACTIVE_PATH)[0].content) == {"organization": "org-beta"}
    assert _form(server.calls("POST", TOKEN_PATH)[0])["grant_type"] == "refresh_token"
    assert login_profile.get().access_token == RENEWED
    assert run.json()["result"] == {
        "profile": "test",
        "organization": "org-beta",
        "display_name": "Beta Labs",
        "role": "editor",
        "token_refreshed": True,
        "cleared_active_project": PROJECT,
    }
    assert "active_project" not in _config(login_profile.config)["profiles"]["test"]


def test_org_switch_to_the_org_the_token_names_keeps_the_project(cli, server, login_profile):
    server.on("GET", ORGS_PATH, answer(200, _listing(active="org-acme")))
    server.on("PUT", ACTIVE_PATH, answer(200, {"active_org": "org-acme"}))
    server.on("POST", TOKEN_PATH, _renewal())
    server.on("GET", ME_PATH, _me_naming({TOKEN: "org-acme", RENEWED: "org-acme"}))

    run = cli("org", "switch", "org-acme")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["cleared_active_project"] is None
    assert _config(login_profile.config)["profiles"]["test"]["active_project"] == PROJECT


def test_org_switch_to_an_org_you_are_not_in_sends_no_write(cli, server, login_profile):
    server.on("GET", ORGS_PATH, answer(200, _listing(active="org-acme")))

    run = cli("org", "switch", "Gamma")

    assert run.code == 1
    error = run.error()
    assert error["code"] == "org_not_member"
    assert error["hint"] == "Pick one of the organizations you belong to."
    assert error["details"]["organizations"] == ["org-acme", "org-beta"]
    assert server.calls("PUT", ACTIVE_PATH) == []


def test_org_switch_to_a_disabled_org_sends_no_write(cli, server, login_profile):
    server.on("GET", ORGS_PATH, answer(200, _listing(active="org-acme", disabled=frozenset({"org-beta"}))))

    run = cli("org", "switch", "Beta Labs")

    assert run.code == 1
    error = run.error()
    assert (error["code"], error["hint"]) == (
        "org_disabled",
        "This organization is disabled; pick another one or contact support.",
    )
    assert server.calls("PUT", ACTIVE_PATH) == []


def test_org_switch_refused_by_the_server_keeps_the_token(cli, server, login_profile):
    server.on("GET", ORGS_PATH, answer(200, _listing(active="org-acme")))
    server.on("GET", ME_PATH, _me_naming({TOKEN: "org-acme"}))
    server.on(
        "PUT",
        ACTIVE_PATH,
        answer(403, {"detail": "You are not a member of that organization.", "code": "org_not_member"}),
    )

    run = cli("org", "switch", "org-beta")

    assert run.code == 1
    assert run.error()["code"] == "org_not_member"
    assert server.calls("POST", TOKEN_PATH) == []
    assert login_profile.get().access_token == TOKEN


def test_a_renewed_token_naming_another_org_is_org_not_in_token(cli, server, login_profile):
    server.on("GET", ORGS_PATH, answer(200, _listing(active="org-acme")))
    server.on("PUT", ACTIVE_PATH, answer(200, {"active_org": "org-beta"}))
    server.on("POST", TOKEN_PATH, _renewal())
    server.on("GET", ME_PATH, _me_naming({TOKEN: "org-acme", RENEWED: "org-acme"}))

    run = cli("org", "switch", "org-beta")

    assert run.code == 1
    error = run.error()
    assert (error["code"], error["details"]["token_organization"]) == ("org_not_in_token", "org-acme")


def test_an_env_token_profile_switches_but_says_the_token_is_not_renewed(cli, server):
    server.on("GET", ORGS_PATH, answer(200, _listing(active="org-acme")))
    server.on("PUT", ACTIVE_PATH, answer(200, {"active_org": "org-beta"}))
    server.on("GET", ME_PATH, _me_naming({TOKEN: "org-acme"}))

    run = cli("org", "switch", "org-beta")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["token_refreshed"] is False
    assert run.events("token_not_refreshed")
    assert server.calls("POST", TOKEN_PATH) == []


def test_an_api_key_profile_cannot_switch_org(cli, server, login_profile, profile_env):
    from conftest import write_login_profile

    write_login_profile(profile_env["config"], SERVER, login_profile.key, auth="api_key")
    login_profile.put(access=TOKEN, expires_in=3600, kind="api_key")

    run = cli("org", "switch", "org-beta")

    assert run.code == 2
    assert run.error()["code"] == "usage_invalid"
    assert server.calls("GET", ORGS_PATH) == []


@pytest.fixture
def signing_in(server, monkeypatch):
    """A device sign-in whose first token names no organization, and a renewal naming org-beta."""
    for name in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    first = jwt(USER_SUB, preferred_username="ada")
    server.on("GET", DISCOVERY_PATH, answer(200, DISCOVERY))
    server.on(
        "POST",
        DEVICE_PATH,
        answer(200, {"device_code": "d", "user_code": "U", "verification_uri": "v", "expires_in": 60, "interval": 0}),
    )
    server.on(
        "POST",
        TOKEN_PATH,
        answer(
            200,
            {"access_token": first, "expires_in": 300, "refresh_token": "r-1", "scope": "openid offline_access"},
        ),
        _renewal(),
    )
    server.on("GET", ME_PATH, _me_naming({first: None, RENEWED: "org-beta"}))
    return server


def test_login_with_org_makes_it_active(cli, signing_in, profile_env):
    signing_in.on("GET", ORGS_PATH, answer(200, _listing(active=None)))
    signing_in.on("PUT", ACTIVE_PATH, answer(200, {"active_org": "org-beta"}))

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file", "--org", "Beta Labs")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["org"] == "org-beta"
    assert json.loads(signing_in.calls("PUT", ACTIVE_PATH)[0].content) == {"organization": "org-beta"}


def test_login_without_org_when_a_choice_is_needed_and_no_terminal_fails_signed_in(cli, signing_in, profile_env):
    signing_in.on("GET", ORGS_PATH, answer(200, _listing(active=None)))

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file")

    assert run.code == 2
    error = run.error()
    assert (error["code"], error["next"]) == ("org_choice_required", "databay org list")
    assert error["recovery"]["side_effects"] == "partial"
    assert signing_in.calls("PUT", ACTIVE_PATH) == []
    assert _config(profile_env["config"])["profiles"]["default"]["auth"] == "login"


def test_login_without_org_on_a_terminal_asks_which_one(cli, signing_in, profile_env, monkeypatch):
    signing_in.on("GET", ORGS_PATH, answer(200, _listing(active=None)))
    signing_in.on("PUT", ACTIVE_PATH, answer(200, {"active_org": "org-beta"}))
    monkeypatch.setattr("databay.cli.org.interactive", lambda _console: True)

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file", stdin="2\n", monkeypatch=monkeypatch)

    assert run.code == 0, run.stderr
    assert json.loads(signing_in.calls("PUT", ACTIVE_PATH)[0].content) == {"organization": "org-beta"}


def test_login_prompt_offers_only_enabled_orgs(cli, signing_in, profile_env, monkeypatch):
    signing_in.on("GET", ORGS_PATH, answer(200, _listing(active=None, disabled=frozenset({"org-acme"}))))
    signing_in.on("PUT", ACTIVE_PATH, answer(200, {"active_org": "org-beta"}))
    monkeypatch.setattr("databay.cli.org.interactive", lambda _console: True)

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file", stdin="1\n", monkeypatch=monkeypatch)

    assert run.code == 0, run.stderr
    assert json.loads(signing_in.calls("PUT", ACTIVE_PATH)[0].content) == {"organization": "org-beta"}


def test_login_with_every_org_disabled_stays_signed_in_and_says_so(cli, signing_in, profile_env):
    disabled = frozenset({"org-acme", "org-beta"})
    signing_in.on("GET", ORGS_PATH, answer(200, _listing(active=None, disabled=disabled)))

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["org"] is None
    assert run.events("no_usable_org")[0]["organizations"] == ["org-acme", "org-beta"]
    assert signing_in.calls("PUT", ACTIVE_PATH) == []


def test_login_with_an_active_org_asks_nothing(cli, signing_in, profile_env):
    signing_in.on("GET", ORGS_PATH, answer(200, _listing(active="org-acme")))

    run = cli("login", "--server", SERVER, "--device", "--token-store", "file")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["org"] == "org-acme"
    assert signing_in.calls("PUT", ACTIVE_PATH) == []


def test_status_asks_for_the_stored_org_and_says_when_one_must_be_chosen(cli, server):
    me = {"sub": "u-1", "organization": [], "principal": "user", "active_org": None, "needs_org_choice": True}
    server.on("GET", ME_PATH, answer(200, me))

    run = cli("status")

    assert run.code == 0, run.stderr
    assert server.calls("GET", ME_PATH)[0].url.params["include_orgs"] == "true"
    assert (run.json()["result"]["org"], run.json()["result"]["needs_org_choice"]) == (None, True)
    assert run.events("org_choice_required")
