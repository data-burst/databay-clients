"""
What every generated SDK resource (``databay.resources.<noun>``) does with a method call.

A method passes its arguments by wire name; an argument left at ``UNSET`` is not sent, so the
server's own default applies. A ``SecretStr`` is revealed only into the request body, and a
generated model is sent as its JSON.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from databay import waits
from databay.errors import DatabayError, DatabayUsageError
from databay.invoke import CallValues, resolve_project, validate

if TYPE_CHECKING:
    from databay.invoke import Session
    from databay.operation import Operation
    from databay.waits import WaitOutcome


class Unset:
    """The value of an argument the caller did not pass."""

    _instance: Unset | None = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance  # type: ignore[return-value]

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = Unset()


@dataclass(slots=True)
class Waited[T]:
    """An answer and the wait that followed it."""

    result: T
    wait: WaitOutcome


def wire_value(value: Any) -> Any:  # noqa: ANN401 — any argument
    """An argument as JSON: a model as its fields, a ``SecretStr`` revealed, lists and dicts element-wise."""
    from pydantic import BaseModel, SecretStr  # noqa: PLC0415

    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_unset=True)
    if isinstance(value, list | tuple):
        return [wire_value(item) for item in value]
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    return value


def _variant_body(op: Operation, body: Any) -> dict[str, Any]:  # noqa: ANN401
    assert op.body is not None
    assert op.body.discriminator is not None
    discriminator = op.body.discriminator
    data = wire_value(body)
    from pydantic import BaseModel  # noqa: PLC0415

    if isinstance(body, BaseModel):
        data[discriminator] = wire_value(getattr(body, discriminator))
    keys = [key for key, _, _ in op.body.variants]
    if not isinstance(data, dict) or data.get(discriminator) not in keys:
        holder = op.body.variant_field or "body"
        raise DatabayUsageError(f"`{op.command}` needs a {holder} whose {discriminator} is one of {keys}")
    return data


def call_values(
    op: Operation,
    args: dict[str, Any],
    *,
    project: str | None,
    idempotency_key: str | None,
    body: Any = UNSET,  # noqa: ANN401 — a variant model or its dict: the whole body, or its ``variant_field``
) -> CallValues:
    """The ``CallValues`` of an SDK call: each argument placed where its operation takes it."""
    values = CallValues(project_flag=project, idempotency_key=idempotency_key)
    project_name = op.project_location[1] if op.project_location else None
    for param in op.params:
        value = args.get(param.name, UNSET)
        if value is UNSET or value is None:
            continue
        if param.name == project_name:
            values.project_explicit = str(value)
            continue
        target = {"path": values.path, "query": values.query, "header": values.headers}[param.location]
        target[param.name] = wire_value(value)
    if op.body is None:
        return values
    if op.body.discriminator is not None and op.body.variant_field is None:
        values.body = _variant_body(op, body)
        values.has_body = True
        return values
    payload: dict[str, Any] = {}
    for param in op.body.fields:
        value = args.get(param.name, UNSET)
        if value is UNSET:
            continue
        if param.name == project_name and value is not None:
            values.project_explicit = str(value)
            continue
        payload[param.name] = wire_value(value)
    if op.body.variant_field is not None:
        payload[op.body.variant_field] = _variant_body(op, body)
    values.body = payload
    values.has_body = True
    return values


class Resource:
    """One noun's operations on a ``Session``; the generated subclasses add one method per verb."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _call(
        self,
        op: Operation,
        args: dict[str, Any],
        *,
        project: str | None = None,
        idempotency_key: str | None = None,
        body: Any = UNSET,  # noqa: ANN401
        wait: bool = False,
        timeout: float | None = None,
    ) -> Any:  # noqa: ANN401 — the operation's model, or a Waited of it
        if op.idempotent and idempotency_key is None:
            # One key per call, so this call's own retries never create twice.
            idempotency_key = str(uuid.uuid4())
        values = call_values(op, args, project=project, idempotency_key=idempotency_key, body=body)
        if wait and op.resumes is not None:
            project = resolve_project(op, values, self._session.active_project)
            self._session.ensure_compatible(op)
            bound = {**values.path, **values.query}
            outcome = waits.run(self._session, op.resumes, bound, timeout_s=timeout, project=project)
            return Waited(validate(op, outcome.status, op.response.model), outcome)
        result = self._session.call(op, values)
        if not wait:
            return result.parsed
        if op.wait is None:
            raise DatabayUsageError(f"`{op.command}` has nothing to wait for")
        bound = waits.bind(op.wait, result.raw, result.path)
        if bound is None:
            return Waited(result.parsed, waits.not_started(op.wait))
        try:
            outcome = waits.run(self._session, op.wait, bound, timeout_s=timeout, project=result.project)
        except DatabayError as exc:
            # The operation itself succeeded: the caller keeps its answer to resume or report.
            exc.result = result.parsed
            raise
        return Waited(result.parsed, outcome)
