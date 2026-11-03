"""
Where a request's bearer token comes from.

The profile alone decides, by its ``auth``; nothing else is consulted:

- ``login``: the credential ``databay login`` stored for a person, renewed with its refresh token;
- ``api_key``: the credential ``databay login --api-key-id`` stored for an org API key, renewed
  with the client-credentials grant;
- ``env``: ``$DATABAY_TOKEN``, read on every request, for a token minted elsewhere.

``$DATABAY_TOKEN`` set while the profile's ``auth`` is ``login`` or ``api_key`` is
``token_source_conflict``: two token sources are in play and the CLI does not pick one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from databay.errors import DatabayProfileError, DatabayUsageError
from databay.settings import ENV_TOKEN

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from databay.profiles import Profile


class TokenProvider(Protocol):
    def token(self) -> str | None:
        """The access token to send now; ``None`` sends no ``Authorization`` header."""
        ...

    def refreshed(self) -> str | None:
        """A new token after the server refused the current one, or ``None`` when this source cannot refresh."""
        ...


@dataclass(frozen=True, slots=True)
class EnvToken:
    """The token in ``$DATABAY_TOKEN``, read on each request; unset or empty is ``token_missing``."""

    variable: str = ENV_TOKEN

    def token(self) -> str:
        value = os.environ.get(self.variable, "")
        if not value:
            raise DatabayProfileError(
                f"the profile reads its token from ${self.variable}, which is not set",
                code="token_missing",
            )
        return value

    def refreshed(self) -> str | None:
        return None


@dataclass(frozen=True, slots=True)
class StaticToken:
    """A token the SDK caller already holds."""

    value: str

    def token(self) -> str:
        return self.value

    def refreshed(self) -> str | None:
        return None


@dataclass(frozen=True, slots=True)
class NoToken:
    """No ``Authorization`` header: the routes a client reads before it has a token (``client-config``)."""

    def token(self) -> None:
        return None

    def refreshed(self) -> str | None:
        return None


def _stored(profile: Profile, http: httpx.Client | None) -> TokenProvider:
    from databay.stored_token import stored_token  # noqa: PLC0415 — loads the OAuth code only when used

    return stored_token(profile, http)


#: ``auth`` value in a profile -> the token source it builds.
TOKEN_SOURCES: dict[str, Callable[[Profile, httpx.Client | None], TokenProvider]] = {
    "env": lambda _profile, _http: EnvToken(),
    "login": _stored,
    "api_key": _stored,
}


def token_provider(profile: Profile, http: httpx.Client | None = None) -> TokenProvider:
    """The profile's token source; ``http`` is the client a renewal is sent with (a new one when ``None``)."""
    if profile.auth != "env" and os.environ.get(ENV_TOKEN):
        raise DatabayUsageError(
            f"${ENV_TOKEN} is set, but profile {profile.name} signs in with auth = {profile.auth!r}: "
            f"unset {ENV_TOKEN}, or use a profile whose auth is env",
            code="token_source_conflict",
            details={"profile": profile.name, "auth": profile.auth},
        )
    return TOKEN_SOURCES[profile.auth](profile, http)
