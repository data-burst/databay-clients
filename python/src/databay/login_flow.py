"""
Signing in: the browser and device flows, and keeping what they return.

- Browser: authorization code with PKCE; Keycloak sends the browser back to
  ``http://127.0.0.1:<random port>/callback``, served here for one request.
- Device: the device authorization grant, also with PKCE (the ``databay-cli`` client requires it on
  every flow). Chosen by ``--device``, and automatically over SSH, on Linux without a display, or
  when Python finds no browser: the loopback callback would not reach the user's real browser.
- API key: the client-credentials grant with a key's client id and secret.

Both person flows ask for ``offline_access`` and fail ``offline_access_not_granted`` without it.
"""

from __future__ import annotations

import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

from databay import oidc
from databay.credentials import Credential
from databay.errors import DatabayError, DatabayProfileError
from databay.settings import SETTINGS

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from databay.cli.console import Console
    from databay.compat import ServerInfo

#: The interval RFC 8628 (section 3.2) sets when the device answer gives none, and the step ``slow_down`` adds.
DEVICE_POLL_DEFAULT_S = 5
DEVICE_SLOW_DOWN_S = 5
CALLBACK_READ_TIMEOUT_S = 5


def device_reason(environ: dict[str, str], platform: str, has_browser: Callable[[], bool]) -> str | None:
    """Why the device flow is used although ``--device`` was not passed; ``None`` keeps the browser flow."""
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return "this is an SSH session"
    if platform.startswith("linux") and not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        return "there is no display"
    if not has_browser():
        return "no local browser"
    return None


def local_browser() -> bool:
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def _refused(exc: oidc.OAuthRefusal) -> DatabayError:
    if exc.error in {"expired_token", "access_denied"}:
        exc.code = exc.error
    return exc


@dataclass(frozen=True, slots=True)
class SignIn:
    """What a flow got: the provider's endpoints, the grant and its subject."""

    endpoints: oidc.Endpoints
    issued: oidc.Grant
    sub: str


def _checked(endpoints: oidc.Endpoints, issued: oidc.Grant, *, offline: bool) -> SignIn:
    if offline and (oidc.OFFLINE_SCOPE not in issued.scope or issued.refresh_token is None):
        raise DatabayProfileError(
            "Keycloak signed you in without offline_access, so the CLI would lose the session within a day",
            code="offline_access_not_granted",
            details={"granted_scope": " ".join(issued.scope)},
        )
    return SignIn(endpoints, issued, oidc.claims(issued.access_token)["sub"])


class _Callback(BaseHTTPRequestHandler):
    """Answers the browser's one return to the loopback; ``server.query`` keeps its parameters."""

    #: Seconds a connection may stay silent: a browser's idle preconnect socket must not hold the
    #: one-request-at-a-time server while the real redirect waits behind it.
    timeout = CALLBACK_READ_TIMEOUT_S

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path != "/callback":
            self.send_error(404)
            return
        self.server.query = {k: v[0] for k, v in parse_qs(url.query).items()}  # type: ignore[attr-defined]
        ok = "code" in self.server.query  # type: ignore[attr-defined]
        text = "Signed in to Databay. You can close this tab." if ok else "Sign-in failed; see the terminal."
        body = f"<!doctype html><title>Databay CLI</title><p>{text}</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401, ARG002 — the stdlib signature
        return


def browser_flow(
    http: httpx.Client,
    endpoints: oidc.Endpoints,
    client_id: str,
    console: Console,
) -> SignIn:
    verifier, challenge = oidc.pkce()
    state = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", 0), _Callback)
    server.query = None  # type: ignore[attr-defined]
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
    url = (
        endpoints.authorization
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": oidc.LOGIN_SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    try:
        console.inform(
            "browser_login",
            {"url": url},
            f"Opening the Databay sign-in page in your browser. If it does not open, visit:\n  {url}",
        )
        webbrowser.open(url)
        deadline = time.monotonic() + SETTINGS.browser_login_timeout_s
        while server.query is None:  # type: ignore[attr-defined]
            left = deadline - time.monotonic()
            if left <= 0:
                raise DatabayProfileError(
                    f"the browser did not return to {redirect_uri} within {SETTINGS.browser_login_timeout_s:g}s",
                    code="login_timeout",
                )
            server.timeout = left
            server.handle_request()
    finally:
        server.server_close()
    query: dict[str, str] = server.query  # type: ignore[attr-defined]
    if query.get("state") != state:
        raise DatabayProfileError(
            "the browser came back with another sign-in's state; nothing was stored", code="login_failed"
        )
    if "error" in query:
        raise _refused(oidc.OAuthRefusal(f"browser sign-in: {query['error']}", error=query["error"]))
    if "code" not in query:
        raise DatabayProfileError(
            "the browser came back without a code or an error; nothing was stored", code="login_failed"
        )
    issued = oidc.grant(
        http,
        endpoints.token,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": query["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    return _checked(endpoints, issued, offline=True)


def device_flow(
    http: httpx.Client,
    endpoints: oidc.Endpoints,
    client_id: str,
    console: Console,
    sleep: Callable[[float], None] = time.sleep,
) -> SignIn:
    verifier, challenge = oidc.pkce()
    started = oidc.start_device(http, endpoints.device, client_id, challenge)
    link = started.get("verification_uri_complete") or started["verification_uri"]
    console.inform(
        "device_login",
        {
            "verification_uri": started["verification_uri"],
            "verification_uri_complete": started.get("verification_uri_complete"),
            "user_code": started["user_code"],
            "expires_in": started["expires_in"],
        },
        f"To sign in, open {link}\nand check that it shows the code {started['user_code']}.",
    )
    interval = float(started.get("interval", DEVICE_POLL_DEFAULT_S))
    deadline = time.monotonic() + started["expires_in"]
    data = {
        "grant_type": oidc.DEVICE_GRANT,
        "client_id": client_id,
        "device_code": started["device_code"],
        "code_verifier": verifier,
    }
    while True:
        if time.monotonic() >= deadline:
            raise DatabayProfileError("the sign-in code expired before it was approved", code="expired_token")
        sleep(interval)
        try:
            issued = oidc.grant(http, endpoints.token, data)
        except oidc.OAuthRefusal as exc:
            if exc.error == "authorization_pending":
                continue
            if exc.error == "slow_down":
                interval += DEVICE_SLOW_DOWN_S
                continue
            raise _refused(exc) from None
        return _checked(endpoints, issued, offline=True)


def api_key_flow(http: httpx.Client, endpoints: oidc.Endpoints, client_id: str, secret: str) -> SignIn:
    issued = oidc.grant(
        http, endpoints.token, {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}
    )
    return _checked(endpoints, issued, offline=False)


def credential(server: str, info: ServerInfo, signed: SignIn, client_id: str, secret: str | None) -> Credential:
    return Credential(
        kind="login" if secret is None else "api_key",
        server=server,
        sub=signed.sub,
        issuer=info.issuer,
        client_id=client_id,
        access_token=signed.issued.access_token,
        expires_at=signed.issued.expires_at,
        refresh_token=signed.issued.refresh_token if secret is None else None,
        client_secret=secret,
    )
