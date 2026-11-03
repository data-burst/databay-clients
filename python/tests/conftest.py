"""
Shared fixtures: an isolated config and state dir with one profile, and a scripted server.

``FakeServer`` sits at the HTTP transport (``httpx.MockTransport``): it answers what a test
scripts and records every request, so a test can say what the client sent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

SERVER = "http://databay.test"
TOKEN = "the-test-token"
PROJECT = "proj-active"
CLIENT_CONFIG = {
    "issuer": "http://keycloak.test/realms/databay",
    "client_id": "databay-cli",
    "product": "core",
    "api_version": "1.0",
}

Handler = Callable[[httpx.Request], httpx.Response]


def answer(status: int, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json=body, headers=headers)


@dataclass
class FakeServer:
    routes: dict[tuple[str, str], list[Handler | httpx.Response]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    client_config: dict[str, Any] = field(default_factory=lambda: dict(CLIENT_CONFIG))

    def on(self, method: str, path: str, *responses: Handler | httpx.Response) -> None:
        """Answer ``method path`` with ``responses`` in turn; the last one repeats."""
        self.routes[(method, path)] = list(responses)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/auth/client-config" and ("GET", request.url.path) not in self.routes:
            return answer(200, self.client_config)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return answer(404, {"detail": "Not Found", "code": "resource_not_found"})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item(request) if callable(item) else item

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def profile_env(tmp_path, monkeypatch) -> dict[str, Any]:
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text(
        f'current_profile = "test"\n\n[profiles.test]\nserver = "{SERVER}"\nauth = "env"\n'
        f'active_project = "{PROJECT}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABAY_CONFIG_DIR", str(config))
    monkeypatch.setenv("DATABAY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DATABAY_TOKEN", TOKEN)
    monkeypatch.delenv("DATABAY_OUTPUT", raising=False)
    # Skills install under the home and the working directory; neither may be the real one.
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    return {"config": config, "state": tmp_path / "state", "home": home, "work": work}


@dataclass
class Run:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)

    def error(self) -> dict[str, Any]:
        lines = [json.loads(line) for line in self.stderr.splitlines() if line.startswith('{"error"')]
        assert len(lines) == 1, self.stderr
        return lines[0]["error"]

    def events(self, name: str) -> list[dict[str, Any]]:
        found = []
        for line in self.stderr.splitlines():
            if line.startswith('{"event"'):
                event = json.loads(line)
                if event["event"] == name:
                    found.append(event)
        return found


@pytest.fixture
def cli(server: FakeServer, profile_env, capsys) -> Callable[..., Run]:
    """Run ``databay <args>`` in process against ``server``; stdout and stderr are captured, not terminals."""
    from databay.cli.main import main

    def run(*args: str, stdin: str | None = None, monkeypatch: pytest.MonkeyPatch | None = None) -> Run:
        if stdin is not None:
            import io

            assert monkeypatch is not None
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        with server.client() as http:
            code = main(list(args), http=http)
        out = capsys.readouterr()
        return Run(code, out.out, out.err)

    return run


ISSUER = "http://keycloak.test/realms/databay"
TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"
REVOCATION_ENDPOINT = f"{ISSUER}/protocol/openid-connect/revoke"
DISCOVERY_PATH = "/realms/databay/.well-known/openid-configuration"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": TOKEN_ENDPOINT,
    "device_authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth/device",
    "revocation_endpoint": REVOCATION_ENDPOINT,
}
USER_SUB = "6f1c2d3e-0000-4000-8000-00000000a11c"


def jwt(sub: str = USER_SUB, **claims: Any) -> str:
    """An unsigned JWT with ``sub`` and ``claims``: the CLI reads the payload of a token it was just issued."""
    import base64

    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part({'sub': sub, **claims})}.sig"


@dataclass
class LoginProfile:
    """Profile ``test``, signed in, with its credential in the file store."""

    config: Any
    key: str

    def store(self) -> Any:
        from databay.credentials import FileStore

        return FileStore(self.config / "credentials")

    def put(self, *, access: str, expires_in: float, refresh: str = "refresh-1", kind: str = "login") -> None:
        import time

        from databay.credentials import Credential

        self.store().put(
            self.key,
            Credential(
                kind=kind,  # type: ignore[arg-type]
                server=SERVER,
                sub=USER_SUB,
                issuer=ISSUER,
                client_id="databay-cli" if kind == "login" else "apikey-acme-1",
                access_token=access,
                expires_at=time.time() + expires_in,
                refresh_token=refresh if kind == "login" else None,
                client_secret=None if kind == "login" else "key-secret-value",
            ),
        )

    def get(self) -> Any:
        return self.store().get(self.key)


def write_login_profile(config: Any, server: str, key: str, *, auth: str = "login") -> None:
    (config / "config.toml").write_text(
        f'current_profile = "test"\n\n[profiles.test]\nserver = "{server}"\nauth = "{auth}"\n'
        f'credential = "{key}"\ntoken_store = "file"\nactive_project = "{PROJECT}"\n',
        encoding="utf-8",
    )


@pytest.fixture
def login_profile(profile_env, server, monkeypatch) -> LoginProfile:
    """The env profile replaced by a login profile whose access token is valid for an hour; the issuer discovers."""
    from databay.credentials import credential_key

    server.on("GET", DISCOVERY_PATH, answer(200, DISCOVERY))
    key = credential_key(SERVER, USER_SUB)
    write_login_profile(profile_env["config"], SERVER, key)
    monkeypatch.delenv("DATABAY_TOKEN")
    profile = LoginProfile(profile_env["config"], key)
    profile.put(access=TOKEN, expires_in=3600)
    return profile
