"""
The SDK client.

::

    from databay import Databay
    from databay.tokens import EnvToken

    with Databay("https://databay.example.com", EnvToken(), project="<project id>") as client:
        pipe = client.pipe.create(source_id=..., table_name="orders", ...)
        done = client.pipe.status(pipe_uuid=pipe.uuid, wait=True)

``Databay.from_profile()`` reads the server, the token source and the active project from a CLI
profile instead. One attribute per noun (``client.pipe``, ``client.backfill_run``) is generated in
``databay.api``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from databay.api import Api
from databay.invoke import Session
from databay.platform_check import ensure_supported
from databay.transport import EventSink, Transport, no_events

if TYPE_CHECKING:
    from types import TracebackType

    import httpx

    from databay.tokens import TokenProvider


class Databay(Api):
    def __init__(
        self,
        server: str,
        token: TokenProvider,
        *,
        project: str | None = None,
        retries: bool = True,
        events: EventSink = no_events,
        http: httpx.Client | None = None,
        profile: str | None = None,
    ) -> None:
        ensure_supported()
        transport = Transport(
            server=server.rstrip("/"), tokens=token, client_kind="sdk", retries=retries, events=events, http=http
        )
        super().__init__(Session(transport, active_project=project, profile=profile))

    @classmethod
    def from_profile(cls, name: str | None = None, **kwargs: object) -> Self:
        """A client for the CLI profile ``name``, else the config's ``current_profile``."""
        from databay.profiles import load_profile  # noqa: PLC0415
        from databay.tokens import token_provider  # noqa: PLC0415

        profile = load_profile(name)
        return cls(
            profile.server,
            token_provider(profile, kwargs.get("http")),  # type: ignore[arg-type]
            project=profile.active_project,
            profile=profile.name,
            **kwargs,  # type: ignore[arg-type]
        )

    def close(self) -> None:
        self._session.transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
