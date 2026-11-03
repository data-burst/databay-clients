"""
Waiting on what an operation started, by polling its declared status operation.

Each status value is pending, success or failure, as the ``Wait`` declares; a value it does not
name (a newer server's) stops the wait with ``unexpected_wait_state``. A 404 is
``wait_target_gone``, or ``wait_feature_disabled`` when it says the feature is off. 5xx, 408, 429 and
network failures are retried within the timeout until ``max_transient_in_a_row`` in a row; any other
error answer stops the wait with its own error.
Running out of time is ``wait_pending``: the same wait can be resumed.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from databay.errors import (
    DatabayError,
    DatabayFeatureDisabled,
    DatabayNetworkError,
    DatabayResponseInvalid,
    DatabayWaitError,
)
from databay.invoke import CallValues, build_request, decode, json_text, pointer
from databay.transport import error_from, is_transient, retry_after_seconds

if TYPE_CHECKING:
    from databay.invoke import Session
    from databay.operation import Operation, Wait
    from databay.transport import Request

#: ``not_started``: the trigger's answer names nothing to wait on.
Outcome = Literal["success", "pending", "failure", "not_started"]


@dataclass(slots=True)
class WaitOutcome:
    outcome: Outcome
    #: The status value last seen, as the declaration spells it; ``None`` before the first answer.
    state: str | None
    #: The last status answer, as JSON.
    status: Any
    elapsed_s: float
    #: The status operation's parameters, by name: what resuming the wait polls again.
    bound: dict[str, str]
    status_operation: str
    #: Why a successful wait ended, when it can end more than one way.
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "outcome": self.outcome,
            "state": self.state,
            "elapsed_s": round(self.elapsed_s, 1),
            "status_operation": self.status_operation,
            "status": self.status,
        }
        return data if self.reason is None else {**data, "reason": self.reason}


def operation(operation_id: str) -> Operation:
    """The generated ``Operation`` with this id."""
    from databay.spec import OPERATIONS  # noqa: PLC0415

    module, attribute = OPERATIONS[operation_id]
    return getattr(importlib.import_module(f"databay.operations.{module}"), attribute)


def not_started(wait: Wait) -> WaitOutcome:
    """The outcome of a wait whose trigger started nothing: the answer has no value to bind."""
    return WaitOutcome("not_started", None, None, 0.0, {}, wait.status_operation)


def bind(wait: Wait, trigger: Any, trigger_path: dict[str, str]) -> dict[str, str] | None:  # noqa: ANN401
    """
    The status operation's parameters, from the trigger's answer or its own path parameters.

    ``None`` when the answer, already validated against its model, leaves a bound field out or
    null: the trigger started nothing to wait on.
    """
    bound: dict[str, str] = {}
    for parameter, source in wait.bind:
        if source.startswith("path:"):
            bound[parameter] = trigger_path[source.removeprefix("path:")]
            continue
        try:
            value = pointer(trigger, source)
        except (KeyError, IndexError, ValueError):
            value = None
        if value is None:
            return None
        bound[parameter] = json_text(value)
    return bound


def classify(wait: Wait, status: Any) -> tuple[Outcome | None, str]:  # noqa: ANN401
    """The outcome a status answer means, and the value it read; ``None`` for a value the wait does not name."""
    value = json_text(pointer(status, wait.field))
    for outcome, values in (("pending", wait.pending), ("success", wait.success), ("failure", wait.failure)):
        if value in values:
            return outcome, value
    for conditional_value, condition in wait.conditional:
        if conditional_value != value:
            continue
        second = json_text(pointer(status, condition.field))
        for outcome, values in (
            ("pending", condition.pending),
            ("success", condition.success),
            ("failure", condition.failure),
        ):
            if second in values:
                return outcome, f"{value} ({condition.field.lstrip('/')}={second})"
        return None, f"{value} ({condition.field.lstrip('/')}={second})"
    return None, value


def _status_values(status_op: Operation, bound: dict[str, str]) -> CallValues:
    values = CallValues()
    for param in status_op.params:
        if param.name not in bound:
            continue
        target = values.path if param.location == "path" else values.query
        target[param.name] = bound[param.name]
    return values


@dataclass(frozen=True, slots=True)
class Reading:
    """What one tick of a wait read, and whether it ends the wait."""

    outcome: Outcome
    state: str
    status: Any
    status_operation: str
    #: Raised to end the wait, with the wait's last state as its ``outcome``.
    stop: DatabayWaitError | None = None
    #: Why a successful wait ended, when it can end more than one way.
    reason: str | None = None


class _TransientFailureError(Exception):
    """A tick's request got an answer a later tick can get past: 5xx, 408, 429 or a network failure."""

    def __init__(self, failure: DatabayError, retry_after: float | None) -> None:
        super().__init__(failure.message)
        self.failure = failure
        self.retry_after = retry_after


#: Sends a tick's request and answers its JSON. It raises what stops the wait, and a transient failure ends the tick.
Fetch = Callable[["Operation", "Request"], Any]


def read_status(wait: Wait, raw: Any, command: str) -> Reading:  # noqa: ANN401
    """A status answer as ``wait`` classifies it; a failure or a value it does not name stops the wait."""
    try:
        outcome, state = classify(wait, raw)
    except (KeyError, IndexError, ValueError):
        raise DatabayResponseInvalid(
            f"{command}: the answer has no {wait.field}", details={"field": wait.field}
        ) from None
    if outcome is None:
        stop = DatabayWaitError(
            f"{command}: {wait.field.lstrip('/')} is {state!r}, a state this CLI does not know",
            code="unexpected_wait_state",
            details={"state": state},
        )
        return Reading("failure", state, raw, wait.status_operation, stop)
    if outcome == "failure":
        reason = _error_text(wait, raw)
        stop = DatabayWaitError(
            f"{command}: ended {state}" + (f": {reason}" if reason else ""),
            details={"state": state, "error": reason},
        )
        return Reading("failure", state, raw, wait.status_operation, stop)
    return Reading(outcome, state, raw, wait.status_operation)


def poll(
    session: Session,
    step: Callable[[Fetch], Reading],
    *,
    command: str,
    status_operation: str,
    bound: dict[str, str],
    limit: float,
    on_state: Callable[[WaitOutcome], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WaitOutcome:
    """
    Run ``step`` once per tick until a reading ends the wait or ``limit`` seconds pass.

    ``step`` sends its requests through the ``Fetch`` it is given, which applies the rules every
    wait shares. A request after a tick's first is not sent once ``limit`` plus the policy's
    ``tick_grace_s`` has passed, so a slow tick cannot pile its requests' timeouts on top of the
    limit. ``command`` names the wait in the timeout message.
    """
    policy = session.transport.settings.wait
    started = clock()
    interval = policy.first_interval_s
    transient = 0
    last = WaitOutcome("pending", None, None, 0.0, bound, status_operation)
    tick_has_sent = False

    def out_of_time(elapsed: float) -> DatabayWaitError:
        last.outcome = "pending"
        last.elapsed_s = elapsed
        return DatabayWaitError(
            f"{command}: still {last.state or 'unknown'} after {round(elapsed)}s",
            code="wait_pending",
            outcome=last,
            details={"state": last.state, "timeout_s": limit},
        )

    def fetch(op: Operation, request: Request) -> Any:  # noqa: ANN401
        nonlocal tick_has_sent
        if tick_has_sent and clock() - started >= limit + policy.tick_grace_s:
            raise out_of_time(clock() - started)
        tick_has_sent = True
        try:
            answer = session.transport.exchange(request, retries=False)
        except DatabayNetworkError as exc:
            raise _TransientFailureError(exc, None) from None
        if answer.status == 404:  # noqa: PLR2004
            last.outcome = "failure"
            last.elapsed_s = clock() - started
            refusal = error_from(answer, request)
            if isinstance(refusal, DatabayFeatureDisabled):
                raise DatabayWaitError(
                    f"{op.command}: the feature {refusal.feature} is off (source: {refusal.source})",
                    code="wait_feature_disabled",
                    hint=refusal.hint,
                    outcome=last,
                    details={"bound": bound, **refusal.details},
                )
            raise DatabayWaitError(
                f"{op.command}: what the wait polled is gone (HTTP 404)",
                code="wait_target_gone",
                outcome=last,
                details={"bound": bound},
            )
        if is_transient(answer.status):
            retry_after = retry_after_seconds(answer.headers.get("retry-after"))
            raise _TransientFailureError(error_from(answer, request), retry_after)
        if answer.status >= 400:  # noqa: PLR2004
            raise error_from(answer, request)
        raw, _ = decode(op, answer)
        return raw

    while True:
        retry_after: float | None = None
        tick_has_sent = False
        try:
            reading = step(fetch)
        except _TransientFailureError as exc:
            retry_after = exc.retry_after
            transient += 1
            if transient >= policy.max_transient_in_a_row:
                exc.failure.details = {**exc.failure.details, "wait": last.to_dict()}
                raise exc.failure from None
        else:
            transient = 0
            changed = reading.state != last.state
            last = WaitOutcome(
                reading.outcome,
                reading.state,
                reading.status,
                clock() - started,
                bound,
                reading.status_operation,
                reading.reason,
            )
            if changed and on_state is not None:
                on_state(last)
            if reading.stop is not None:
                reading.stop.outcome = last
                raise reading.stop
            if reading.outcome == "success":
                return last
        elapsed = clock() - started
        if elapsed >= limit:
            raise out_of_time(elapsed)
        pause = interval
        if retry_after is not None:
            pause = min(max(interval, retry_after), session.transport.settings.retry.retry_after_cap_s)
        sleep(min(pause, limit - elapsed))
        interval = min(interval * policy.backoff, policy.max_interval_s)


def run(
    session: Session,
    wait: Wait,
    bound: dict[str, str],
    *,
    timeout_s: float | None,
    project: str | None = None,
    on_state: Callable[[WaitOutcome], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WaitOutcome:
    """Poll until success; raise ``DatabayWaitError`` (or the error that stopped it) otherwise."""
    policy = session.transport.settings.wait
    limit = timeout_s if timeout_s is not None else (wait.timeout_s or policy.default_timeout_s)
    status_op = operation(wait.status_operation)
    request = build_request(status_op, _status_values(status_op, bound), project if status_op.project else None)
    return poll(
        session,
        lambda fetch: read_status(wait, fetch(status_op, request), status_op.command),
        command=status_op.command,
        status_operation=wait.status_operation,
        bound=bound,
        limit=limit,
        on_state=on_state,
        clock=clock,
        sleep=sleep,
    )


def _error_text(wait: Wait, raw: Any) -> str | None:  # noqa: ANN401
    if wait.error_field is None:
        return None
    try:
        value = pointer(raw, wait.error_field)
    except (KeyError, IndexError, ValueError):
        return None
    return None if value is None else json_text(value)
