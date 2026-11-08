"""
Running one generated command: its inputs, the confirmation, the call, the wait and the output.

Order matters: every input is read and checked (flags, secrets, the project) before any request,
and a destructive command is confirmed before anything is sent.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from databay import waits
from databay.cli.console import Console, output_format
from databay.cli.output import emit
from databay.errors import (
    DatabayCancelled,
    DatabayError,
    DatabayNotReady,
    DatabayUsageError,
    DatabayWaitError,
)
from databay.invoke import CallValues, Session, resolve_project
from databay.transport import Transport

if TYPE_CHECKING:
    from collections.abc import Callable

    from databay.cli.flags import Flag
    from databay.operation import Body, Operation, Param, Wait
    from databay.profiles import Profile
    from databay.tokens import TokenProvider


def _usage(message: str, code: str = "usage_invalid") -> DatabayUsageError:
    return DatabayUsageError(message, code=code)


def _parse_json(flag: str, text: str) -> Any:  # noqa: ANN401
    try:
        return json.loads(text)
    except ValueError as exc:
        raise _usage(f"{flag} takes JSON: {exc}") from None


#: A flag with choices reads its value as text (``click.Choice``): an integer or number enum sends a number.
_NUMBERS: dict[str | None, type] = {"integer": int, "number": float}


def _convert(param: Param, flag: str, value: Any) -> Any:  # noqa: ANN401
    if param.kind == "json":
        return _parse_json(flag, value)
    if param.kind == "list":
        cast = _NUMBERS.get(param.item_kind)
        return [cast(v) for v in value] if cast else list(value)
    cast = _NUMBERS.get(param.kind)
    return cast(value) if cast else value


class Stdin:
    """stdin, which one input at most may read."""

    def __init__(self) -> None:
        self.claimed_by: str | None = None

    def read(self, flag: str) -> str:
        if self.claimed_by is not None:
            raise _usage(f"{flag} and {self.claimed_by} both read stdin; pass one of them another way")
        self.claimed_by = flag
        return sys.stdin.read()


def _secret(param: Param, values: dict[str, Any], stdin: Stdin) -> Any:  # noqa: ANN401
    env_flag, stdin_flag = f"--{param.flag}-env", f"--{param.flag}-stdin"
    env_name = values.get(f"{param.identifier}_env")
    from_stdin = values.get(f"{param.identifier}_stdin")
    if env_name and from_stdin:
        raise _usage(f"pass {param.name} with {env_flag} or {stdin_flag}, not both")
    text: str | None = None
    if env_name:
        text = os.environ.get(env_name, "")
        if not text:
            raise _usage(f"{env_flag} names ${env_name}, which is not set or empty", "secret_required")
    elif from_stdin:
        text = stdin.read(stdin_flag).rstrip("\r\n")
        if not text:
            raise _usage(f"{stdin_flag}: stdin was empty", "secret_required")
    elif param.required:
        if not sys.stdin.isatty():
            raise _usage(f"{param.name} is a secret: pass {env_flag} NAME or {stdin_flag}", "secret_required")
        text = click.prompt(param.name, hide_input=True, err=True)
    if text is None:
        return None
    return _parse_json(env_flag, text) if param.kind == "json" else text


def _body_file(path: str, stdin: Stdin) -> Any:  # noqa: ANN401
    if path == "-":
        return _parse_json("--file", stdin.read("--file -"))
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise _usage(f"--file {path}: {exc.strerror}") from None
    return _parse_json(f"--file {path}", text)


def _with_variant(spec: Body, variant: str, body: Any, fields: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    """``body`` with the variant's ``fields`` and discriminator in place; a file's own discriminator must agree."""
    if not isinstance(body, dict):
        raise _usage("--file must hold a JSON object")
    holder = body if spec.variant_field is None else body.setdefault(spec.variant_field, {})
    if not isinstance(holder, dict):
        raise _usage(f"--file's {spec.variant_field} must hold a JSON object")
    assert spec.discriminator is not None
    holder.update(fields)
    given = holder.get(spec.discriminator, variant)
    if given != variant:
        raise _usage(f"--file has {spec.discriminator}={given!r}, but the command is `{variant}`")
    holder[spec.discriminator] = variant
    return body


def call_values(op: Operation, variant: str | None, flags: list[Flag], values: dict[str, Any]) -> CallValues:
    """The command line as ``CallValues``; a missing required input is a usage error naming its flag."""
    call = CallValues(project_flag=values.get("project"), idempotency_key=values.get("idempotency_key"))
    location = op.project_location
    stdin = Stdin()
    body: dict[str, Any] = {}
    variant_body: dict[str, Any] = {}
    in_variant = {p.name for p in op.body.variant(variant)[1]} if op.body is not None and variant is not None else ()
    body_flags: list[str] = []
    missing: list[str] = []
    for flag in flags:
        param = flag.param
        if param is None or flag.role == "secret_stdin":
            continue
        is_project = location is not None and location == (param.location, param.name)
        if flag.role == "secret_env":
            if values.get("file") is not None:
                secret_flags = {
                    f"{param.identifier}_env": flag.name,
                    f"{param.identifier}_stdin": f"--{param.flag}-stdin",
                }
                body_flags += [name for dest, name in secret_flags.items() if values.get(dest)]
                continue
            value = _secret(param, values, stdin)
            if value is not None:
                (variant_body if param.name in in_variant else body)[param.name] = value
                body_flags.append(flag.name)
            elif param.required:
                missing.append(flag.name)
            continue
        raw = values.get(flag.dest)
        if raw is None or raw == ():
            if flag.required and not (param.location == "body" and values.get("file") is not None):
                missing.append(flag.name)
            continue
        if is_project:
            call.project_explicit = str(raw)
            if param.location == "body":
                body_flags.append(flag.name)
            continue
        value = _convert(param, flag.name, raw)
        if param.location == "body":
            (variant_body if param.name in in_variant else body)[param.name] = value
            body_flags.append(flag.name)
        else:
            {"path": call.path, "query": call.query, "header": call.headers}[param.location][param.name] = value
    if missing:
        raise _usage(f"`{op.command}` needs {', '.join(missing)}")
    if op.body is None:
        return call
    if values.get("file") is not None:
        if body_flags:
            raise _usage(f"pass the body with --file or with flags ({', '.join(body_flags)}), not both")
        body = _body_file(values["file"], stdin)
        if location is not None and location[0] == "body" and isinstance(body, dict) and location[1] in body:
            # The file's own project id is an explicit one: --project must agree with it. A null one is
            # not given, as the SDK reads None.
            named = body.pop(location[1])
            if named is not None:
                call.project_explicit = str(named)
    call.body = body if variant is None else _with_variant(op.body, variant, body, variant_body)
    call.has_body = True
    return call


def _confirm(op: Operation, call: CallValues, project: str | None, yes: bool, console: Console) -> None:
    if not op.destructive or yes:
        return
    if not (sys.stdin.isatty() and console.stdout.isatty()):
        raise _usage(f"`{op.command}` removes or overwrites state: pass --yes to confirm", "confirmation_required")
    target = " ".join(str(v) for v in call.path.values())
    where = f" in project {project}" if project else ""
    answer = click.prompt(
        f"{op.verb} {op.noun} {target}{where}? [y/N]".replace("  ", " "),
        default="",
        show_default=False,
        err=True,
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise DatabayCancelled(f"{op.command} cancelled; nothing was sent")


def _resume_command(
    wait: Wait, bound: dict[str, str], timeout: float | None, profile: str | None, project: str | None
) -> str:
    """The status command that resumes ``wait``: same target, project and profile; nothing starts again."""
    status_op = waits.operation(wait.status_operation)
    words = ["databay", status_op.noun, status_op.verb]
    words += [bound[p.name] for p in status_op.path_params]
    for param in status_op.params:
        if param.location == "query" and param.name in bound:
            words += [f"--{param.flag}", bound[param.name]]
    words.append("--wait")
    if timeout is not None:
        words += ["--timeout", f"{timeout:g}"]
    if status_op.project is not None and project is not None:
        words += ["--project", project]
    if profile is not None:
        words += ["--profile", profile]
    return shlex.join(words)


def open_session(values: dict[str, Any], console: Console, state: dict[str, Any]) -> tuple[Session, Profile]:
    """A session as the profile ``--profile`` (else ``current_profile``) names, with the common flags applied."""
    from databay.profiles import load_profile  # noqa: PLC0415
    from databay.tokens import token_provider  # noqa: PLC0415

    profile = load_profile(values.get("profile"))
    return session_for(profile, token_provider(profile, state.get("http")), values, console, state), profile


def session_for(
    profile: Profile, tokens: TokenProvider, values: dict[str, Any], console: Console, state: dict[str, Any]
) -> Session:
    """A session as ``profile`` with ``tokens``, the common flags applied."""
    from databay.profiles import login_command  # noqa: PLC0415

    transport = Transport(
        server=profile.server,
        tokens=tokens,
        client_kind="cli",
        retries=not values.get("no_retry"),
        verbose=bool(values.get("verbose")),
        events=console.event,
        http=state.get("http"),
        login_command=login_command(profile),
    )
    return Session(transport, active_project=profile.active_project, profile=profile.name)


def run(ctx: click.Context, op: Operation, variant: str | None, flags: list[Flag], values: dict[str, Any]) -> None:
    """Run ``op`` from parsed command-line ``values``; any failure leaves as a ``DatabayError``."""
    state: dict[str, Any] = ctx.ensure_object(dict)
    console = Console(output=output_format(values.get("output"), sys.stdout), verbose=bool(values.get("verbose")))
    state["console"] = console
    if values.get("timeout") is not None and not values.get("wait"):
        raise _usage("--timeout applies to --wait only")
    call = call_values(op, variant, flags, values)
    if op.idempotent and call.idempotency_key is None:
        call.idempotency_key = str(uuid.uuid4())
    state["idempotency_key"] = call.idempotency_key
    session, profile = open_session(values, console, state)
    state["context"] = {"server": profile.server, "profile": profile.name, "request_id": session.transport.request_id}
    project = resolve_project(op, call, session.active_project)
    state["context"]["project"] = project
    _confirm(op, call, project, bool(values.get("yes")), console)
    explicit_profile = values.get("profile")
    try:
        session.ensure_compatible(op)
        resuming = bool(values.get("wait")) and op.resumes is not None
        # From here a write may be out: an interrupt can no longer promise that nothing changed.
        state["sent"] = op.method != "get" and not resuming
        state["reading"] = not state["sent"]
        if resuming:
            _run_resume(op, call, session, console, values, explicit_profile, project, state)
            return
        try:
            result = session.call(op, call)
        except DatabayNotReady as exc:
            # A not-ready verdict is still an answer: print it, then exit 9.
            emit(
                console,
                {"result": exc.result, **({"project": project} if project else {})},
                op.columns,
                op.response.rows,
            )
            raise
        state["context"]["request_id"] = result.request_id
        envelope: dict[str, Any] = {"result": result.raw}
        if result.project is not None:
            envelope["project"] = result.project
        if call.idempotency_key is not None:
            envelope["idempotency_key"] = call.idempotency_key
        wait = op.wait
        bound = waits.bind(wait, result.raw, result.path) if wait is not None else None
        if wait is not None and values.get("wait"):
            if bound is None:
                envelope["wait"] = waits.not_started(wait).to_dict()
                emit(console, envelope, op.columns, op.response.rows)
                return
            resume = _resume_command(wait, bound, values.get("timeout"), explicit_profile, project)
            _wait(session, wait, bound, values.get("timeout"), project, console, envelope, op, resume, state)
            return
        if wait is not None and bound is not None:
            envelope["next"] = _resume_command(wait, bound, None, explicit_profile, project)
        emit(console, envelope, op.columns, op.response.rows)
    finally:
        session.transport.close()


def _wait(
    session: Session,
    wait: Wait,
    bound: dict[str, str],
    timeout: float | None,
    project: str | None,
    console: Console,
    envelope: dict[str, Any],
    op: Operation,
    resume: str,
    state: dict[str, Any],
    *,
    from_status: bool = False,
) -> None:
    """
    Wait, then print the envelope with the wait's outcome, also when the wait does not succeed.

    Whatever stops the wait, the operation's answer is printed. ``from_status`` makes the last status
    answer the result: a status command run with ``--wait``.
    """

    def poll(on_state: Callable[[waits.WaitOutcome], None]) -> waits.WaitOutcome:
        return waits.run(session, wait, bound, timeout_s=timeout, project=project, on_state=on_state)

    report_wait(poll, console, envelope, op.columns, op.response.rows, resume, state, from_status=from_status)


def report_wait(
    poll: Callable[[Callable[[waits.WaitOutcome], None]], waits.WaitOutcome],
    console: Console,
    envelope: dict[str, Any],
    columns: tuple[str, ...],
    rows: str | None,
    resume: str,
    state: dict[str, Any],
    *,
    from_status: bool = False,
    then: Callable[[waits.WaitOutcome], str] | None = None,
) -> None:
    """
    Run ``poll``, reporting each state on stderr, then print the envelope, also when the wait does not succeed.

    A wait that can still end well (out of time, a transient failure, an interrupt) carries ``next``,
    the command that resumes it without starting anything again. ``then`` names ``next`` after a success.
    """
    state["next"] = resume

    def on_state(outcome: waits.WaitOutcome) -> None:
        console.progress(outcome.state, outcome.elapsed_s, outcome.status_operation)

    def record(outcome: waits.WaitOutcome) -> None:
        data = outcome.to_dict()
        if from_status:
            envelope["result"] = data.pop("status")
        envelope["wait"] = data

    def print_answer() -> None:
        # A status command's only answer is the status: with none read yet there is nothing to print.
        if envelope["result"] is not None or not from_status:
            emit(console, envelope, columns, rows)

    try:
        outcome = poll(on_state)
    except DatabayError as exc:
        if isinstance(exc, DatabayWaitError) and exc.outcome is not None:
            record(exc.outcome)
        if exc.recovery.action == "retry" or exc.exit_code == 3:  # noqa: PLR2004 — exit 3: sign in, then resume
            # Only a stop that a later poll can get past is worth resuming: a transient one, or a token
            # that a new sign-in replaces.
            envelope["next"] = resume
            exc.next_command = resume
        print_answer()
        raise
    except KeyboardInterrupt:
        envelope["next"] = resume
        print_answer()
        raise
    record(outcome)
    if then is not None:
        envelope["next"] = then(outcome)
    emit(console, envelope, columns, rows)


def _run_resume(
    op: Operation,
    call: CallValues,
    session: Session,
    console: Console,
    values: dict[str, Any],
    profile: str | None,
    project: str | None,
    state: dict[str, Any],
) -> None:
    """``--wait`` on a status command: poll it as the wait that started it would; the result is its last answer."""
    assert op.resumes is not None
    bound = {**{k: str(v) for k, v in call.path.items()}, **{k: str(v) for k, v in call.query.items()}}
    resume = _resume_command(op.resumes, bound, values.get("timeout"), profile, project)
    envelope: dict[str, Any] = {"result": None}
    if project is not None:
        envelope["project"] = project
    _wait(
        session,
        op.resumes,
        bound,
        values.get("timeout"),
        project,
        console,
        envelope,
        op,
        resume,
        state,
        from_status=True,
    )
