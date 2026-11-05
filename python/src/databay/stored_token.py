"""
The token source of a profile that signed in: ``auth = "login"`` (a person) or ``auth = "api_key"``.

The access token is read from the profile's credential with its expiry. With less than
``refresh_margin_s`` left it is renewed under the credential's file lock, so parallel commands
renew once: a process that waited for the lock reads the store again and finds the new token.
A login renews with its refresh token, an API key with the client-credentials grant, both at the
token endpoint the credential's issuer names in its discovery document.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from databay import oidc
from databay.credentials import Credential, key_sub, open_store
from databay.errors import DatabayProfileError
from databay.locks import locked
from databay.profiles import login_command
from databay.settings import SETTINGS

if TYPE_CHECKING:
    import httpx

    from databay.credentials import TokenStore
    from databay.profiles import Profile

#: OAuth errors that mean the stored credential no longer works: only a new sign-in helps.
DEAD_CREDENTIAL = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})


def login_required(profile: Profile, why: str) -> DatabayProfileError:
    return DatabayProfileError(
        f"profile {profile.name}: {why}; sign in again", code="login_required", next_command=login_command(profile)
    )


@dataclass
class StoredToken:
    profile: Profile
    key: str
    store: TokenStore
    http: httpx.Client | None = None
    #: The access token this source last handed out: a 401 on it means it, not a newer one, is refused.
    sent: str | None = None
    #: The access token and its expiry as last read, so a command's requests read the store once.
    _held: tuple[str, float] | None = None
    #: The issuer's endpoints, discovered at this source's first renewal.
    _endpoints: oidc.Endpoints | None = None

    def _load(self) -> Credential:
        record = self.store.get(self.key)
        if record is None or record.sub != key_sub(self.key) or record.kind != self.profile.auth:
            raise DatabayProfileError(
                f"profile {self.profile.name} names credential {self.key}, which the {self.store.kind} store "
                "does not hold for that user",
                code="credential_missing",
                next_command=login_command(self.profile),
            )
        return record

    def token(self) -> str:
        if self._held is not None and self._held[1] - time.time() > SETTINGS.refresh_margin_s:
            return self._hand_out(self._held[0], self._held[1])
        record = self._load()
        if record.expires_at - time.time() <= SETTINGS.refresh_margin_s:
            with locked(self.key):
                record = self._load()
                if record.expires_at - time.time() <= SETTINGS.refresh_margin_s:
                    record = self._renew(record)
        return self._hand_out(record.access_token, record.expires_at)

    def refreshed(self) -> str | None:
        with locked(self.key):
            record = self._load()
            if record.access_token == self.sent:
                record = self._renew(record)
        return self._hand_out(record.access_token, record.expires_at)

    def renewed(self) -> str:
        """A token issued now, whatever the current one's expiry: it carries a change the server just stored."""
        with locked(self.key):
            record = self._renew(self._load())
        return self._hand_out(record.access_token, record.expires_at)

    def _hand_out(self, token: str, expires_at: float) -> str:
        self.sent = token
        self._held = (token, expires_at)
        return token

    def _renew(self, record: Credential) -> Credential:
        # Credential.from_json refuses a login without its refresh token and a key without its secret.
        if record.kind == "login":
            data = {"grant_type": "refresh_token", "client_id": record.client_id, "refresh_token": record.refresh_token}
        else:
            data = {
                "grant_type": "client_credentials",
                "client_id": record.client_id,
                "client_secret": record.client_secret,
            }
        try:
            if self.http is not None:
                issued = self._grant(self.http, record.issuer, data)
            else:
                from databay.transport import new_http_client  # noqa: PLC0415

                with new_http_client(SETTINGS) as http:
                    issued = self._grant(http, record.issuer, data)
        except oidc.OAuthRefusal as exc:
            if exc.error in DEAD_CREDENTIAL:
                why = f"the identity provider refused the stored credential ({exc.error})"
                raise login_required(self.profile, why) from None
            raise
        renewed = record.with_tokens(issued.access_token, issued.expires_at, issued.refresh_token)
        self.store.put(self.key, renewed)
        return renewed

    def _grant(self, http: httpx.Client, issuer: str, data: dict[str, str]) -> oidc.Grant:
        if self._endpoints is None:
            self._endpoints = oidc.discover(http, issuer)
        return oidc.grant(http, self._endpoints.token, data)


def stored_token(profile: Profile, http: httpx.Client | None) -> StoredToken:
    """The source for a signed-in profile; one without a credential is ``credential_missing``."""
    if profile.credential is None or profile.token_store is None:
        raise DatabayProfileError(
            f"profile {profile.name} is not signed in",
            code="credential_missing",
            next_command=login_command(profile),
        )
    return StoredToken(profile, profile.credential, open_store(profile.token_store), http)
