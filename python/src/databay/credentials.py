"""
Stored credentials: what ``databay login`` keeps for one server and one user, and where.

A credential is keyed ``databay-cli:<server>:<sub>``; profiles name it in ``credential``. It
holds the access token with its expiry and either a refresh token (a person's login) or an API
key's client secret. It lives in one of two stores, which the profile names in ``token_store``:

- ``keyring``: the OS keyring, the default. No usable keyring is ``keyring_unavailable``.
- ``file``: ``<config dir>/credentials/<sha256 of the key>.json``, mode 0600, chosen with
  ``--token-store file``. A file anyone else may read is ``token_file_insecure``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, fields, replace
from typing import TYPE_CHECKING, Literal, Protocol

from databay.errors import DatabayProfileError
from databay.paths import config_dir

if TYPE_CHECKING:
    from pathlib import Path

CredentialKind = Literal["login", "api_key"]
TokenStoreKind = Literal["keyring", "file"]
TOKEN_STORES: tuple[TokenStoreKind, ...] = ("keyring", "file")
KEY_PREFIX = "databay-cli"
KEYRING_SERVICE = "databay-cli"


def credential_key(server: str, sub: str) -> str:
    return f"{KEY_PREFIX}:{server}:{sub}"


def key_sub(key: str) -> str:
    """The ``sub`` a credential key names: what follows its last ``:`` (a URL holds colons, a sub none)."""
    return key.rpartition(":")[2]


#: Fields a credential saved by an earlier CLI also holds. They are dropped on read: the endpoints come
#: from the issuer's discovery document when they are needed, so a server that moves one never breaks
#: a stored login.
PINNED_ENDPOINT_FIELDS = frozenset({"token_endpoint", "revocation_endpoint"})


@dataclass(frozen=True, slots=True)
class Credential:
    kind: CredentialKind
    server: str
    sub: str
    #: Where renewal and logout discover Keycloak's endpoints.
    issuer: str
    client_id: str
    access_token: str
    #: Unix time the access token expires at.
    expires_at: float
    #: A person's login only.
    refresh_token: str | None = None
    #: An API key only: the key's client secret.
    client_secret: str | None = None

    def with_tokens(self, access_token: str, expires_at: float, refresh_token: str | None) -> Credential:
        """A copy holding newly issued tokens; a refresh answer without a refresh token keeps the current one."""
        return replace(
            self,
            access_token=access_token,
            expires_at=expires_at,
            refresh_token=refresh_token if refresh_token is not None else self.refresh_token,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str, where: str) -> Credential:
        names = {f.name for f in fields(cls)}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise _unreadable(where, "not JSON") from exc
        if isinstance(data, dict) and set(data) == names | PINNED_ENDPOINT_FIELDS:
            data = {name: data[name] for name in names}
        if not isinstance(data, dict) or set(data) != names:
            raise _unreadable(where, f"expected the fields {sorted(names)}")
        kind = data["kind"]
        if kind not in ("login", "api_key"):
            raise _unreadable(where, f"unknown kind {kind!r}")
        renews_with = "refresh_token" if kind == "login" else "client_secret"
        if not isinstance(data[renews_with], str) or not data[renews_with]:
            raise _unreadable(where, f"a {kind} credential without its {renews_with}")
        return cls(**data)


def _unreadable(where: str, problem: str) -> DatabayProfileError:
    return DatabayProfileError(
        f"the stored credential in {where} is unreadable ({problem}); sign in again with databay login",
        code="credential_missing",
    )


class TokenStore(Protocol):
    kind: TokenStoreKind

    def ready(self) -> None:
        """Raise when the store cannot be used, before anyone signs in."""
        ...

    def get(self, key: str) -> Credential | None: ...

    def put(self, key: str, credential: Credential) -> None: ...

    def delete(self, key: str) -> None: ...


def _keyring_unavailable(reason: str) -> DatabayProfileError:
    return DatabayProfileError(
        f"no usable OS keyring ({reason}); pass --token-store file to keep tokens in a 0600 file instead",
        code="keyring_unavailable",
    )


class KeyringStore:
    kind: TokenStoreKind = "keyring"

    def _backend(self):  # noqa: ANN202 — a keyring backend
        import keyring  # noqa: PLC0415 — keyring scans its backends at import
        from keyring.backends import fail  # noqa: PLC0415

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring):
            raise _keyring_unavailable("no keyring backend is installed or running")
        return backend

    def _call(self, action: str, *args: str, absent_ok: bool = False) -> str | None:
        """
        Call the backend; a keyring error is ``keyring_unavailable``.

        ``absent_ok`` lets a delete of an entry that is not there succeed: ``PasswordDeleteError``
        is a ``KeyringError`` too.
        """
        from keyring.errors import KeyringError, PasswordDeleteError  # noqa: PLC0415

        backend = self._backend()
        try:
            return getattr(backend, action)(KEYRING_SERVICE, *args)
        except PasswordDeleteError:
            if absent_ok:
                return None
            raise _keyring_unavailable(f"PasswordDeleteError on {action}") from None
        except KeyringError as exc:
            raise _keyring_unavailable(f"{type(exc).__name__} on {action}") from exc

    def ready(self) -> None:
        self._backend()

    def get(self, key: str) -> Credential | None:
        text = self._call("get_password", key)
        return None if text is None else Credential.from_json(text, f"the keyring entry {key}")

    def put(self, key: str, credential: Credential) -> None:
        self._call("set_password", key, credential.to_json())

    def delete(self, key: str) -> None:
        self._call("delete_password", key, absent_ok=True)


class FileStore:
    kind: TokenStoreKind = "file"

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory if directory is not None else config_dir() / "credentials"

    def ready(self) -> None:
        return

    def path(self, key: str) -> Path:
        return self.directory / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def get(self, key: str) -> Credential | None:
        path = self.path(key)
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return None
        if stat.S_IMODE(mode) & 0o077:
            raise DatabayProfileError(
                f"{path} holds tokens and others may read it (mode {stat.S_IMODE(mode):o}); run chmod 600 {path}",
                code="token_file_insecure",
                details={"path": str(path)},
            )
        return Credential.from_json(path.read_text(encoding="utf-8"), str(path))

    def put(self, key: str, credential: Credential) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(key)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # A temp file a crashed run left keeps its old mode through O_CREAT.
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(credential.to_json())
        tmp.replace(path)

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


def open_store(kind: str) -> TokenStore:
    if kind == "keyring":
        return KeyringStore()
    if kind == "file":
        return FileStore()
    raise DatabayProfileError(f"token_store must be one of {list(TOKEN_STORES)}, got {kind!r}", code="profile_invalid")
