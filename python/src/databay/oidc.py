"""
The OAuth requests the CLI sends to the identity provider (Keycloak): discovery, token grants, revocation.

Every grant answer is checked the same way: a 2xx with ``access_token`` and ``expires_in``, or an
OAuth error (``{"error": ...}``) raised as ``OAuthRefusal`` naming it. Tokens never appear in an
error message or an event.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from databay.errors import DatabayError, DatabayNetworkError, DatabayResponseInvalid, DatabayServerUnavailable

if TYPE_CHECKING:
    import httpx

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
LOGIN_SCOPE = "openid offline_access"
OFFLINE_SCOPE = "offline_access"


class OAuthRefusal(DatabayError):
    """The provider answered an OAuth error; ``error`` is its ``error`` field (``invalid_grant``, ``slow_down``, …)."""

    code = "login_failed"

    def __init__(self, message: str, *, error: str, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(message, **kwargs)
        self.error = error


@dataclass(frozen=True, slots=True)
class Endpoints:
    issuer: str
    authorization: str
    token: str
    device: str
    #: ``None`` when the provider advertises no revocation endpoint.
    revocation: str | None


@dataclass(frozen=True, slots=True)
class Grant:
    access_token: str
    expires_at: float
    refresh_token: str | None
    scope: tuple[str, ...]


def pkce() -> tuple[str, str]:
    """A code verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def claims(jwt: str) -> dict[str, Any]:
    """
    The payload of a JWT the token endpoint just issued to this client.

    Read, not verified: the token came straight from the provider over the connection the CLI
    opened, and the server checks every token it is sent.
    """
    try:
        payload = jwt.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise DatabayResponseInvalid("the access token the provider issued is not a JWT") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sub"), str):
        raise DatabayResponseInvalid("the access token the provider issued has no sub")
    return data


def _post(http: httpx.Client, url: str, data: dict[str, str]) -> httpx.Response:
    import httpx  # noqa: PLC0415

    try:
        return http.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.TransportError as exc:
        raise DatabayNetworkError(f"POST {url}: could not reach the identity provider ({type(exc).__name__})") from exc


def _transient(status: int) -> bool:
    return status >= 500 or status == 429  # noqa: PLR2004


def _json(response: httpx.Response, where: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        if _transient(response.status_code):
            raise DatabayServerUnavailable(f"{where}: HTTP {response.status_code} from the identity provider")
        raise DatabayResponseInvalid(f"{where}: HTTP {response.status_code} without a JSON object")
    return body


def _refusal(response: httpx.Response, body: dict[str, Any], where: str) -> DatabayError:
    """The error a non-200 answer is: transient for a 5xx or 429 whatever its body, else the OAuth error it names."""
    error = body.get("error")
    if _transient(response.status_code):
        return DatabayServerUnavailable(
            f"{where}: HTTP {response.status_code} from the identity provider"
            + (f" ({error})" if isinstance(error, str) else ""),
            status=response.status_code,
        )
    if not isinstance(error, str):
        raise DatabayResponseInvalid(f"{where}: HTTP {response.status_code} without an OAuth error")
    description = body.get("error_description")
    text = f"{where}: the identity provider answered {error}" + (
        f" ({description})" if isinstance(description, str) else ""
    )
    return OAuthRefusal(text, error=error, status=response.status_code)


def discover(http: httpx.Client, issuer: str) -> Endpoints:
    """The issuer's endpoints from its OpenID discovery document."""
    import httpx  # noqa: PLC0415

    url = f"{issuer}/.well-known/openid-configuration"
    try:
        response = http.get(url, headers={"Accept": "application/json"})
    except httpx.TransportError as exc:
        raise DatabayNetworkError(f"GET {url}: could not reach the identity provider ({type(exc).__name__})") from exc
    if response.status_code != 200:  # noqa: PLR2004
        raise DatabayServerUnavailable(f"GET {url}: HTTP {response.status_code}")
    body = _json(response, f"GET {url}")
    required = ("issuer", "authorization_endpoint", "token_endpoint", "device_authorization_endpoint")
    missing = [name for name in required if not isinstance(body.get(name), str)]
    if missing:
        raise DatabayResponseInvalid(f"GET {url}: the discovery document has no {missing}")
    if body["issuer"] != issuer:
        raise DatabayResponseInvalid(
            f"GET {url}: the discovery document names issuer {body['issuer']}, not {issuer}",
            details={"issuer": body["issuer"], "expected": issuer},
        )
    revocation = body.get("revocation_endpoint")
    return Endpoints(
        issuer=issuer,
        authorization=body["authorization_endpoint"],
        token=body["token_endpoint"],
        device=body["device_authorization_endpoint"],
        revocation=revocation if isinstance(revocation, str) else None,
    )


def grant(http: httpx.Client, token_endpoint: str, data: dict[str, str], *, now: float | None = None) -> Grant:
    """POST one grant to the token endpoint; an OAuth error is ``OAuthRefusal``."""
    issued_at = time.time() if now is None else now
    where = f"{data['grant_type']} grant"
    response = _post(http, token_endpoint, data)
    body = _json(response, where)
    if response.status_code != 200:  # noqa: PLR2004
        raise _refusal(response, body, where)
    access, expires_in = body.get("access_token"), body.get("expires_in")
    if not isinstance(access, str) or not isinstance(expires_in, int | float):
        raise DatabayResponseInvalid(f"{where}: the answer has no access_token and expires_in")
    refresh = body.get("refresh_token")
    scope = body.get("scope")
    return Grant(
        access_token=access,
        expires_at=issued_at + float(expires_in),
        refresh_token=refresh if isinstance(refresh, str) else None,
        scope=tuple(scope.split()) if isinstance(scope, str) else (),
    )


def start_device(http: httpx.Client, endpoint: str, client_id: str, challenge: str) -> dict[str, Any]:
    """Start a device authorization; the answer holds the user code and the verification links."""
    where = "device authorization"
    response = _post(
        http,
        endpoint,
        {"client_id": client_id, "scope": LOGIN_SCOPE, "code_challenge": challenge, "code_challenge_method": "S256"},
    )
    body = _json(response, where)
    if response.status_code != 200:  # noqa: PLR2004
        raise _refusal(response, body, where)
    missing = [
        name
        for name, kind in (("device_code", str), ("user_code", str), ("verification_uri", str), ("expires_in", int))
        if not isinstance(body.get(name), kind)
    ]
    if missing:
        raise DatabayResponseInvalid(f"{where}: the answer has no {missing}")
    return body


def revoke(http: httpx.Client, endpoint: str, client_id: str, refresh_token: str) -> None:
    """Revoke ``refresh_token`` (RFC 7009); a refusal raises ``OAuthRefusal`` or a transport error."""
    where = "token revocation"
    response = _post(
        http, endpoint, {"client_id": client_id, "token": refresh_token, "token_type_hint": "refresh_token"}
    )
    if response.status_code == 200:  # noqa: PLR2004
        return
    raise _refusal(response, _json(response, where), where)
