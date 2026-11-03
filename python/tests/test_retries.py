"""Which requests are retried, how long the client waits between tries, and what it reports."""

from __future__ import annotations

import httpx
import pytest
from conftest import SERVER, TOKEN, FakeServer, answer

from databay.errors import DatabayError
from databay.tokens import StaticToken
from databay.transport import Request, Transport


def _transport(server: FakeServer, events: list, sleeps: list, *, retries: bool = True) -> Transport:
    return Transport(
        server=SERVER,
        tokens=StaticToken(TOKEN),
        client_kind="sdk",
        retries=retries,
        events=lambda name, fields: events.append((name, fields)),
        http=server.client(),
        sleep=sleeps.append,
    )


@pytest.mark.parametrize("status", [408, 429, 502, 503, 504])
def test_a_get_is_retried_on_a_transient_status_with_backoff(server, status):
    server.on("GET", "/x", answer(status, {"detail": "busy", "code": "auth_unavailable"}), answer(200, {}))
    events, sleeps = [], []

    answered = _transport(server, events, sleeps).send(Request("GET", "/x"))

    assert answered.status == 200
    assert answered.attempts == 2
    assert sleeps == [1.0]
    assert [name for name, _ in events] == ["retry"]
    assert events[0][1]["status"] == status


def test_retries_stop_after_three_and_the_error_names_the_attempts(server):
    server.on("GET", "/x", answer(503, {"detail": "busy", "code": "auth_unavailable"}))
    sleeps: list[float] = []

    with pytest.raises(DatabayError) as caught:
        _transport(server, [], sleeps).send(Request("GET", "/x"))

    assert caught.value.code == "auth_unavailable"
    assert caught.value.attempts == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_after_is_honoured_and_capped(server):
    server.on(
        "GET",
        "/x",
        answer(429, {"detail": "slow down", "code": "precheck_rate_limited"}, {"Retry-After": "7"}),
        answer(429, {"detail": "slow down", "code": "precheck_rate_limited"}, {"Retry-After": "120"}),
        answer(200, {}),
    )
    sleeps: list[float] = []

    _transport(server, [], sleeps).send(Request("GET", "/x"))

    assert sleeps == [7.0, 30.0]


def test_a_write_with_an_idempotency_key_is_retried(server):
    server.on("POST", "/x", answer(503, {"detail": "busy", "code": "auth_unavailable"}), answer(200, {}))

    answered = _transport(server, [], []).send(Request("POST", "/x", headers={"Idempotency-Key": "k1"}))

    assert answered.attempts == 2
    assert [r.headers["idempotency-key"] for r in server.calls("POST", "/x")] == ["k1", "k1"]


def test_a_write_without_a_key_is_sent_once(server):
    server.on("POST", "/x", answer(503, {"detail": "busy", "code": "auth_unavailable"}), answer(200, {}))

    with pytest.raises(DatabayError):
        _transport(server, [], []).send(Request("POST", "/x"))

    assert len(server.calls("POST", "/x")) == 1


def test_a_refusal_is_not_retried(server):
    server.on("GET", "/x", answer(409, {"detail": "busy", "code": "rebuild_in_progress"}), answer(200, {}))

    with pytest.raises(DatabayError):
        _transport(server, [], []).send(Request("GET", "/x"))

    assert len(server.calls("GET", "/x")) == 1


def test_no_retry_sends_once(server):
    server.on("GET", "/x", answer(503, {"detail": "busy", "code": "auth_unavailable"}), answer(200, {}))

    with pytest.raises(DatabayError):
        _transport(server, [], [], retries=False).send(Request("GET", "/x"))

    assert len(server.calls("GET", "/x")) == 1


def test_a_connect_failure_of_a_get_is_retried(server):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server.on("GET", "/x", refuse, answer(200, {}))

    assert _transport(server, [], []).send(Request("GET", "/x")).attempts == 2


def test_every_request_says_who_sent_it(server):
    server.on("GET", "/x", answer(200, {}))

    _transport(server, [], []).send(Request("GET", "/x"))

    sent = server.calls("GET", "/x")[0]
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["user-agent"] == "databay-python/0.1.0 (sdk)"
    assert sent.headers["traceparent"].startswith("00-")


@pytest.mark.parametrize("value", ["NaN", "inf", "soon"])
def test_a_retry_after_that_is_not_a_finite_delay_keeps_the_backoff(server, value):
    server.on(
        "GET",
        "/x",
        answer(503, {"detail": "busy", "code": "auth_unavailable"}, {"Retry-After": value}),
        answer(200, {}),
    )
    sleeps: list[float] = []

    _transport(server, [], sleeps).send(Request("GET", "/x"))

    assert sleeps == [1.0]
