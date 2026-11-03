"""``databay profile use|list``, ``databay status`` and ``databay project use``: local state and who you are."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import click

from databay.cli.common import common_options, console_for
from databay.cli.handwritten import HANDWRITTEN
from databay.settings import OUTPUT_FORMATS

if TYPE_CHECKING:
    from pathlib import Path

    from databay.agent_skill import Installed
    from databay.cli.console import Console

PROFILE_COLUMNS = ("name", "current", "server", "auth", "user", "token_store", "active_project")


@click.group("profile", short_help="Switch between and list profiles")
def profile() -> None:
    """Profiles in config.toml: each is one server, its sign-in and its active project."""


@profile.command("use")
@click.argument("name")
@click.option("--output", type=click.Choice(OUTPUT_FORMATS), default=None, help="json, table or csv")
@click.pass_context
def profile_use(ctx: click.Context, name: str, output: str | None) -> None:
    """Make NAME the current profile."""
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.errors import DatabayProfileError  # noqa: PLC0415
    from databay.paths import config_file  # noqa: PLC0415
    from databay.profiles import edit_config  # noqa: PLC0415

    console, _ = console_for(ctx, {"output": output})

    def change(data: dict[str, Any]) -> None:
        if name not in data["profiles"]:
            known = ", ".join(sorted(data["profiles"])) or "none"
            raise DatabayProfileError(f"no profile {name!r} in {config_file()} (profiles: {known})")
        data["current_profile"] = name

    edit_config(change)
    emit(console, {"result": {"current_profile": name}}, ())


@profile.command("list")
@click.option("--output", type=click.Choice(OUTPUT_FORMATS), default=None, help="json, table or csv")
@click.pass_context
def profile_list(ctx: click.Context, output: str | None) -> None:
    """Every profile, and which one is current."""
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.credentials import key_sub  # noqa: PLC0415
    from databay.profiles import all_profiles  # noqa: PLC0415

    console, _ = console_for(ctx, {"output": output})
    current, profiles = all_profiles()
    rows = [
        {
            "name": p.name,
            "current": p.name == current,
            "server": p.server,
            "auth": p.auth,
            "user": key_sub(p.credential) if p.credential else None,
            "token_store": p.token_store,
            "active_project": p.active_project,
        }
        for p in profiles
    ]
    emit(console, {"result": rows}, PROFILE_COLUMNS)


@click.command("status", short_help=HANDWRITTEN["status"][2])
@common_options
@click.pass_context
def status(ctx: click.Context, **values: Any) -> None:  # noqa: ANN401
    """
    Show the profile, the server, and who you are signed in as.

    Prints the server's product and API version, the user and org, the active project, which
    output rule applied, and every installed agent skill. It calls the server, so it also checks the
    sign-in works. skill.state is skill_outdated when an installed skill is from another CLI version.
    """
    from databay.cli.console import output_rule  # noqa: PLC0415
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.cli.runner import open_session  # noqa: PLC0415
    from databay.invoke import CallValues  # noqa: PLC0415
    from databay.operations.me import ME_GET  # noqa: PLC0415

    console, state = console_for(ctx, values)
    _, rule = output_rule(values.get("output"), sys.stdout)
    session, chosen = open_session(values, console, state)
    state["context"] = {"server": chosen.server, "profile": chosen.name, "project": chosen.active_project}
    try:
        me = session.call(ME_GET, CallValues()).parsed
        assert session.server is not None
        info, _ = session.server
    finally:
        session.transport.close()
    result = {
        "profile": chosen.name,
        "server": chosen.server,
        "product": info.product,
        "api_version": info.api_version,
        "auth": chosen.auth,
        "user": me.preferred_username or me.email,
        "sub": me.sub,
        "principal": str(me.principal),
        "org": me.organization[0] if me.organization else None,
        "active_project": chosen.active_project,
        "output": {"format": console.output, "rule": rule},
        "skill": _skill_status(console),
    }
    emit(console, {"result": result}, ())


def _skill_status(console: Console) -> dict[str, Any]:
    """
    Every installed copy of the agent skill and whether it matches this CLI's version.

    ``state`` is ``current``, ``skill_outdated`` (a copy records another version, or none) or
    ``not_installed``; each outdated copy is also a ``skill_outdated`` notice on stderr.
    """
    from pathlib import Path  # noqa: PLC0415

    from databay.agent_skill import find_installed  # noqa: PLC0415
    from databay.version import CLIENT_VERSION  # noqa: PLC0415

    cwd = Path.cwd()
    copies = []
    for copy in find_installed(home=Path.home(), cwd=cwd):
        is_outdated = copy.version != CLIENT_VERSION
        copies.append(
            {
                "target": copy.target,
                "scope": copy.scope,
                "path": str(copy.path),
                "version": copy.version,
                "problem": copy.problem,
                "outdated": is_outdated,
            }
        )
        if is_outdated:
            command = _reinstall_command(copy, cwd)
            console.inform(
                "skill_outdated",
                {
                    "path": str(copy.path),
                    "installed_version": copy.version,
                    "problem": copy.problem,
                    "cli_version": CLIENT_VERSION,
                    "next": command,
                },
                f"skill_outdated: {copy.path} is for CLI {copy.version or f'an unknown version ({copy.problem})'}, "
                f"this CLI is {CLIENT_VERSION}; run {command}",
            )
    if not copies:
        state = "not_installed"
    elif any(c["outdated"] for c in copies):
        state = "skill_outdated"
    else:
        state = "current"
    return {"state": state, "cli_version": CLIENT_VERSION, "installed": copies}


def _reinstall_command(copy: Installed, cwd: Path) -> str:
    """The command that replaces ``copy``; a project copy above ``cwd`` is reinstalled from its own folder."""
    import shlex  # noqa: PLC0415

    command = f"databay skill install --target {copy.target} --scope {copy.scope} --force"
    project = copy.path.parents[2]
    if copy.scope == "project" and project != cwd:
        return f"cd {shlex.quote(str(project))} && {command}"
    return command


@click.command("use", short_help="Set the profile's active project")
@click.argument("project_id")
@common_options
@click.pass_context
def project_use(ctx: click.Context, project_id: str, **values: Any) -> None:  # noqa: ANN401
    """
    Make PROJECT_ID the active project of the profile, after checking it is one you can see.

    Ids only: `databay project list` shows names. A project being deleted is refused like one you cannot see.
    """
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.cli.runner import open_session  # noqa: PLC0415
    from databay.errors import api_error  # noqa: PLC0415
    from databay.invoke import CallValues  # noqa: PLC0415
    from databay.operations.project import PROJECT_LIST  # noqa: PLC0415
    from databay.profiles import edit_config  # noqa: PLC0415

    console, state = console_for(ctx, values)
    session, chosen = open_session(values, console, state)
    state["context"] = {"server": chosen.server, "profile": chosen.name, "project": project_id}
    try:
        visible = session.call(PROJECT_LIST, CallValues()).parsed
    finally:
        session.transport.close()
    match = next((p for p in visible if p.id == project_id and p.status == "active"), None)
    if match is None:
        raise api_error(
            "project_not_found",
            f"project {project_id} is not an active project you can see on {chosen.server}",
            status=404,
            details={"project_id": project_id},
        )

    def change(data: dict[str, Any]) -> None:
        data["profiles"][chosen.name]["active_project"] = project_id

    edit_config(change)
    emit(
        console,
        {"result": {"profile": chosen.name, "active_project": project_id, "name": match.name}, "project": project_id},
        (),
    )
