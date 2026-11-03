"""
Calling one operation: the project, the request, the answer's model, and the verdict.

``Session`` is what the SDK resources and the CLI commands share. It checks the server once
(``databay.compat``), places the project where the operation takes it, sends the request and
validates the answer against the generated model before anyone reads it.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from databay import compat
from databay.errors import DatabayNotReady, DatabayResponseInvalid, DatabayUsageError, api_error
from databay.transport import IDEMPOTENCY_HEADER, PROJECT_HEADER, Request

if TYPE_CHECKING:
    from databay.compat import ServerInfo
    from databay.operation import Operation, Verdict
    from databay.transport import Answer, Transport


@dataclass(slots=True)
class CallValues:
    """
    The caller's inputs to one operation, by wire name.

    ``project_explicit`` is a project id the caller passed as the operation's own project
    parameter; ``project_flag`` is ``--project`` (or the SDK's ``project=``).
    """

    path: dict[str, str] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    has_body: bool = False
    project_explicit: str | None = None
    project_flag: str | None = None
    idempotency_key: str | None = None


@dataclass(slots=True)
class Result:
    raw: Any
    parsed: Any
    project: str | None
    request_id: str
    #: The path values the request was sent with, which a wait may bind.
    path: dict[str, str]


def resolve_project(op: Operation, values: CallValues, active: str | None) -> str | None:
    """
    The project ``op`` is sent for.

    An explicit id and ``--project`` that differ are ``project_conflict``; otherwise the explicit
    id, then ``--project``, then the profile's active project. None of them is ``no_active_project``.
    """
    if op.project is None:
        return None
    explicit, flag = values.project_explicit, values.project_flag
    if explicit and flag and explicit != flag:
        raise api_error(
            "project_conflict",
            f"the project id argument {explicit} and --project {flag} differ",
            details={"argument": explicit, "flag": flag},
        )
    chosen = explicit or flag or active
    if not chosen:
        raise DatabayUsageError(
            f"`{op.command}` acts on a project: pass --project <project_id> or set the profile's active_project",
            code="no_active_project",
        )
    return chosen


def pointer(document: Any, path: str) -> Any:  # noqa: ANN401 — any JSON value
    """The value at a JSON pointer (``/a/b``); ``KeyError`` when a step is missing."""
    value = document
    for raw_step in path.lstrip("/").split("/"):
        step = raw_step.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(step)]
        elif isinstance(value, dict):
            value = value[step]
        else:
            raise KeyError(path)
    return value


def json_text(value: Any) -> str:  # noqa: ANN401
    """How a declaration spells a value: a string as is, anything else as JSON (``true``, ``null``, ``3``)."""
    import json  # noqa: PLC0415

    return value if isinstance(value, str) else json.dumps(value)


def build_request(op: Operation, values: CallValues, project: str | None) -> Request:
    path_values = dict(values.path)
    query = dict(values.query)
    headers = dict(values.headers)
    body = values.body
    location = op.project_location
    if location is not None and project is not None:
        where, name = location
        if where == "header":
            headers[PROJECT_HEADER] = project
        elif where == "path":
            path_values[name] = project
        elif where == "query":
            query[name] = project
        else:
            body = {**(body or {}), name: project}
    missing = [p.name for p in op.path_params if p.name not in path_values]
    if missing:
        raise DatabayUsageError(f"`{op.command}` needs {', '.join(missing)}")
    path = op.path
    for name, value in path_values.items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    if values.idempotency_key is not None:
        headers[IDEMPOTENCY_HEADER] = values.idempotency_key
    pairs = [
        (name, json_text(item))
        for name, value in query.items()
        for item in (value if isinstance(value, list) else [value])
    ]
    return Request(
        method=op.method.upper(),
        path=path,
        query=pairs,
        headers=headers,
        json_body=body,
        has_body=body is not None or values.has_body,
        secret_fields=op.secret_fields,
    )


def model_type(reference: str) -> Any:  # noqa: ANN401 — a model class or a type alias
    module, _, name = reference.partition(":")
    return getattr(importlib.import_module(f"databay.models.{module}"), name)


def validate(op: Operation, raw: Any, reference: str | None) -> Any:  # noqa: ANN401
    """``raw`` as the generated model, or ``response_invalid`` naming the field, never the body."""
    if reference is None:
        return raw
    from pydantic import TypeAdapter, ValidationError  # noqa: PLC0415

    try:
        return TypeAdapter(model_type(reference)).validate_python(raw)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "(the body)"
        raise DatabayResponseInvalid(
            f"`{op.command}`: the answer does not fit {reference.partition(':')[2]} at {where} ({first['type']})",
            details={"operation": op.operation_id, "field": where, "error": first["type"]},
        ) from None


def decode(op: Operation, answer: Answer) -> tuple[Any, Any]:
    """The answer as ``(raw JSON or text, validated value)``."""
    kind = op.response.kind
    if kind == "empty" or answer.status == 204:  # noqa: PLR2004
        return None, None
    if kind == "text":
        text = answer.text()
        return text, text
    try:
        raw = answer.json()
    except ValueError:
        raise DatabayResponseInvalid(
            f"`{op.command}`: HTTP {answer.status} answered a body that is not JSON",
            details={"operation": op.operation_id},
        ) from None
    return raw, validate(op, raw, op.response.model)


def check_verdict(op: Operation, verdict: Verdict, raw: Any) -> None:  # noqa: ANN401
    try:
        value = json_text(pointer(raw, verdict.field))
    except (KeyError, IndexError, ValueError):
        raise DatabayResponseInvalid(
            f"`{op.command}`: the answer has no {verdict.field}", details={"operation": op.operation_id}
        ) from None
    if value in verdict.ok:
        return
    if value in verdict.not_ready:
        raise DatabayNotReady(
            f"`{op.command}`: {verdict.field.lstrip('/')} is {value}", result=raw, details={"value": value}
        )
    raise DatabayResponseInvalid(
        f"`{op.command}`: {verdict.field.lstrip('/')} is {value!r}, which this CLI does not classify; upgrade it",
        details={"operation": op.operation_id, "value": value},
    )


@dataclass
class Session:
    """One caller on one server: a transport, the profile's active project and the server's version."""

    transport: Transport
    active_project: str | None = None
    profile: str | None = None
    #: The server's client-config and whether it came from the cache, once read.
    server: tuple[ServerInfo, bool] | None = None

    def ensure_compatible(self, op: Operation) -> None:
        self.server = compat.ensure_compatible(self.transport, self.server, op)

    def call(self, op: Operation, values: CallValues, *, check_verdict_: bool = True) -> Result:
        project = resolve_project(op, values, self.active_project)
        request = build_request(op, values, project)
        self.ensure_compatible(op)
        answer = self.transport.send(request)
        raw, parsed = decode(op, answer)
        if check_verdict_ and op.verdict is not None:
            check_verdict(op, op.verdict, raw)
        path = {p.name: values.path[p.name] for p in op.path_params if p.name in values.path}
        location = op.project_location
        if location is not None and location[0] == "path" and project is not None:
            path[location[1]] = project
        return Result(raw=raw, parsed=parsed, project=project, request_id=answer.request_id, path=path)
