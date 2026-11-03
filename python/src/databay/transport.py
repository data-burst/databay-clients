"""
One HTTP exchange with a Databay server: headers, retries, verbose logs, and errors by ``code``.

A GET, or a write sent with an ``Idempotency-Key``, is retried on 429/502/503/504 and on connect or
read failures (``RetryPolicy``); any other write is sent once. Each retry is reported to the event
sink. An error answer becomes the exception its ``code`` names (``databay.errors``).
"""

from __future__ import annotations

import json
import math
import secrets
import time
import uuid
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol

from databay import redact
from databay.error_catalog import UNCODED_404_HINT
from databay.errors import (
    DatabayError,
    DatabayFeatureDisabled,
    DatabayNetworkError,
    DatabayProfileError,
    DatabayServerUnavailable,
    DatabayUncodedError,
    DatabayUnknownCodeError,
)
from databay.settings import SETTINGS, ClientSettings
from databay.version import CLIENT_VERSION

if TYPE_CHECKING:
    import httpx

    from databay.tokens import TokenProvider

CORRELATION_HEADER = "x-correlation-id"
IDEMPOTENCY_HEADER = "Idempotency-Key"
PROJECT_HEADER = "X-Databay-Project-ID"
#: 4xx answers that say "later", not "wrong": request timeout and rate limiting.
TRANSIENT_4XX = frozenset({408, 429})


def is_transient(status: int) -> bool:
    """Whether an error status says "try later": any 5xx, 408 or 429. A wait polls through these."""
    return status >= 500 or status in TRANSIENT_4XX  # noqa: PLR2004


class EventSink(Protocol):
    def __call__(self, event: str, fields: dict[str, Any]) -> None: ...


def no_events(_event: str, _fields: dict[str, Any]) -> None:
    return None


@dataclass(slots=True)
class Answer:
    status: int
    headers: dict[str, str]
    content: bytes
    request_id: str
    attempts: int

    @property
    def is_json(self) -> bool:
        return self.headers.get("content-type", "").split(";")[0].strip().endswith("json")

    def json(self) -> Any:  # noqa: ANN401 — any JSON value
        return json.loads(self.content)

    def text(self) -> str:
        return self.content.decode("utf-8")


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: list[tuple[str, str]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    json_body: Any = None
    has_body: bool = False
    #: Body fields ``--verbose`` replaces with ``<redacted>``.
    secret_fields: frozenset[str] = frozenset()

    @property
    def retryable(self) -> bool:
        return self.method == "GET" or IDEMPOTENCY_HEADER in self.headers


@dataclass
class Transport:
    """
    Sends requests to one server as one caller.

    ``client_kind`` is ``cli`` or ``sdk`` in the ``User-Agent``. ``http`` is the httpx client to
    send with; tests hand one built on ``httpx.MockTransport``.
    """

    server: str
    tokens: TokenProvider
    client_kind: str
    retries: bool = True
    verbose: bool = False
    events: EventSink = no_events
    settings: ClientSettings = SETTINGS
    http: httpx.Client | None = None
    #: One id for every request this transport sends, echoed by the server as ``x-correlation-id``.
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sleep: Any = time.sleep
    #: The command a ``login_required`` names as ``next``: the profile's login, when there is a profile.
    login_command: str | None = None
    _owned: httpx.Client | None = field(default=None, init=False, repr=False)

    def _client(self) -> httpx.Client:
        if self.http is not None:
            return self.http
        if self._owned is None:
            self._owned = new_http_client(self.settings)
        return self._owned

    def close(self) -> None:
        """Close the client this transport opened; one the caller handed in stays the caller's."""
        if self._owned is not None:
            self._owned.close()
            self._owned = None

    def _headers(self, request: Request) -> dict[str, str]:
        token = self.tokens.token()
        return {
            **({"Authorization": f"Bearer {token}"} if token is not None else {}),
            "User-Agent": f"databay-python/{CLIENT_VERSION} ({self.client_kind})",
            "Accept": "application/json",
            CORRELATION_HEADER: self.request_id,
            "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01",
            **request.headers,
        }

    def send(self, request: Request, *, retries: bool | None = None) -> Answer:
        """Send ``request``, retrying it when it is safe and allowed; raise its error answer."""
        answer = self.exchange(request, retries=retries)
        if answer.status >= 400:  # noqa: PLR2004
            raise error_from(answer, request)
        return answer

    def exchange(self, request: Request, *, retries: bool | None = None) -> Answer:
        """
        Send ``request`` and return the last answer, error or not; a network failure raises.

        A 401 is sent once more after the token source refreshes, when it can; a 401 on the refreshed
        token is ``login_required``.
        """
        answer = self._exchange(request, retries=retries)
        if answer.status == 401 and self.tokens.refreshed() is not None:  # noqa: PLR2004
            answer = self._exchange(request, retries=retries)
            if answer.status == 401:  # noqa: PLR2004
                raise DatabayProfileError(
                    f"{request.method} {request.path}: {self.server} refused the token even after a refresh; "
                    "sign in again with databay login",
                    code="login_required",
                    status=401,
                    attempts=answer.attempts,
                    next_command=self.login_command,
                )
        return answer

    def _exchange(self, request: Request, *, retries: bool | None) -> Answer:
        allowed = (self.retries if retries is None else retries) and request.retryable
        policy = self.settings.retry
        attempt = 0
        while True:
            attempt += 1
            failure: DatabayError | None = None
            answer: Answer | None = None
            try:
                answer = self._once(request, attempt)
            except DatabayNetworkError as exc:
                failure = exc
            retryable = failure is not None or (answer is not None and answer.status in policy.statuses)
            if not (allowed and retryable and attempt <= policy.max_retries):
                if failure is not None:
                    failure.attempts = attempt
                    raise failure
                assert answer is not None
                return answer
            delay = self._delay(answer, attempt)
            self.events(
                "retry",
                {
                    "attempt": attempt + 1,
                    "status": answer.status if answer else None,
                    "error": failure.code if failure else None,
                    "delay_s": delay,
                    "request_id": self.request_id,
                },
            )
            self.sleep(delay)

    def _delay(self, answer: Answer | None, attempt: int) -> float:
        policy = self.settings.retry
        backoff = policy.backoff_s[min(attempt, len(policy.backoff_s)) - 1]
        after = retry_after_seconds(answer.headers.get("retry-after") if answer else None)
        return backoff if after is None else min(after, policy.retry_after_cap_s)

    def _once(self, request: Request, attempt: int) -> Answer:
        import httpx  # noqa: PLC0415

        headers = self._headers(request)
        url = f"{self.server}{request.path}"
        content = json.dumps(request.json_body).encode() if request.has_body else None
        if content is not None:
            headers["Content-Type"] = "application/json"
        started = time.monotonic()
        if self.verbose:
            self._log_request(request, url, headers, attempt)
        try:
            response = self._client().request(
                request.method, url, params=request.query or None, headers=headers, content=content
            )
        except httpx.TimeoutException as exc:
            raise DatabayNetworkError(
                f"{request.method} {request.path}: the server did not answer in time ({type(exc).__name__})"
            ) from exc
        except httpx.TransportError as exc:
            raise DatabayNetworkError(
                f"{request.method} {request.path}: could not reach {self.server} ({type(exc).__name__})"
            ) from exc
        answer = Answer(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            content=response.content,
            request_id=response.headers.get(CORRELATION_HEADER, self.request_id),
            attempts=attempt,
        )
        if self.verbose:
            self.events(
                "response",
                {
                    "status": answer.status,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "bytes": len(answer.content),
                    "request_id": answer.request_id,
                },
            )
        return answer

    def _log_request(self, request: Request, url: str, headers: dict[str, str], attempt: int) -> None:
        fields: dict[str, Any] = {
            "method": request.method,
            "url": url,
            "query": request.query,
            "headers": redact.headers(headers),
            "attempt": attempt,
        }
        if request.has_body:
            fields["body"] = redact.body(request.json_body, request.secret_fields)
        self.events("request", fields)


def new_http_client(settings: ClientSettings) -> httpx.Client:
    """The httpx client requests go through unless a caller hands one in: the settings' timeouts, no redirects."""
    import httpx  # noqa: PLC0415 — imported only when a command talks to a server

    timeout = httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s)
    return httpx.Client(timeout=timeout, follow_redirects=False)


def retry_after_seconds(value: str | None) -> float | None:
    """
    ``Retry-After`` as seconds, from a number or an HTTP date.

    ``None`` when the header is absent or unparsable: the caller's own backoff applies then.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(seconds, 0.0) if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(when.timestamp() - time.time(), 0.0)


def _message(detail: Any, status: int, code: str | None) -> str:  # noqa: ANN401 — any JSON value
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        # The envelope some services answer under ``detail`` (docs/workspace-api.md).
        return detail["message"]
    return f"HTTP {status}" + (f" {code}" if code else "")


def error_from(answer: Answer, request: Request) -> DatabayError:
    """The exception an error answer names: by its ``code``, or the named error for a body that does not fit."""
    from databay.error_codes import EXCEPTIONS  # noqa: PLC0415 — loaded only on an error
    from databay.feature_header import FEATURE_HEADER, parse_feature_header  # noqa: PLC0415 — likewise

    where = f"{request.method} {request.path}"
    common: dict[str, Any] = {"status": answer.status, "attempts": answer.attempts}
    body: Any = None
    if answer.is_json:
        try:
            body = answer.json()
        except ValueError:
            body = None
    if not isinstance(body, dict):
        if is_transient(answer.status):
            return DatabayServerUnavailable(f"{where}: HTTP {answer.status} without a Databay error body", **common)
        # A 4xx no Databay service answered (a wrong server URL, a proxy's page) is not transient.
        return DatabayError(
            f"{where}: HTTP {answer.status} without a Databay error body; check the profile's server URL",
            code="non_databay_answer",
            **common,
        )
    code = body.get("code")
    detail = body.get("detail")
    extra = {k: v for k, v in body.items() if k not in {"code", "detail"}}
    if code is None:
        is_404 = answer.status == 404  # noqa: PLR2004
        off = parse_feature_header(answer.headers.get(FEATURE_HEADER)) if is_404 else None
        if off is not None:
            return DatabayFeatureDisabled(
                f"{where}: the feature {off.feature} is off (source: {off.source})",
                feature=off.feature,
                source=off.source,
                **common,
            )
        return DatabayUncodedError(
            f"{where}: HTTP {answer.status} without a code: {_message(detail, answer.status, None)}",
            hint=UNCODED_404_HINT if is_404 else None,
            details={"detail": detail},
            **common,
        )
    if not isinstance(code, str) or code not in EXCEPTIONS:
        return DatabayUnknownCodeError(
            f"{where}: the server answered code {code!r}, which this CLI does not know",
            details={"server_code": code, "detail": detail},
            **common,
        )
    if not isinstance(detail, str):
        extra["detail"] = detail
    docs = detail.get("docs_url") if isinstance(detail, dict) else None
    return EXCEPTIONS[code](
        _message(detail, answer.status, code),
        details=extra,
        support_ref=body.get("support_ref") if isinstance(body.get("support_ref"), str) else None,
        docs=docs if isinstance(docs, str) else None,
        **common,
    )
