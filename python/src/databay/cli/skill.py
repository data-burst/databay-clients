"""``databay skill install``: copy the agent skill that ships with this CLI to where an agent loads it."""

from __future__ import annotations

from pathlib import Path

import click

from databay.agent_skill import SCOPES
from databay.cli.common import console_for
from databay.cli.handwritten import HANDWRITTEN
from databay.settings import DEFAULT_SKILL_SCOPE, OUTPUT_FORMATS


@click.group("skill", short_help=HANDWRITTEN["skill"][2])
def skill() -> None:
    """The agent skill: how an agent drives this CLI, with the command and error references."""


INSTALL_HELP = """Install the skill where the agent loads it, recording this CLI's version in its SKILL.md.

\b
claude: ~/.claude/skills/databay (user) or .claude/skills/databay (project)
codex:  ~/.agents/skills/databay (user) or .agents/skills/databay (project)

A copy from another CLI version is replaced only with --force; without it the command exits 2
(skill_exists) and names both versions."""


@skill.command("install", help=INSTALL_HELP)
@click.option("--target", required=True, metavar="AGENT", help="the agent to install for: claude or codex")
@click.option(
    "--scope",
    type=click.Choice(SCOPES),
    default=DEFAULT_SKILL_SCOPE,
    show_default=True,
    help="user: under your home, for every project; project: under the current directory",
)
@click.option("--force", is_flag=True, default=False, help="replace a copy installed by another CLI version")
@click.option("--output", type=click.Choice(OUTPUT_FORMATS), default=None, help="json, table or csv")
@click.pass_context
def install(ctx: click.Context, target: str, scope: str, force: bool, output: str | None) -> None:
    from databay.agent_skill import install as install_skill  # noqa: PLC0415
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.version import CLIENT_VERSION  # noqa: PLC0415

    console, _ = console_for(ctx, {"output": output})
    path, previous = install_skill(
        target,
        scope,  # type: ignore[arg-type] — click checked the choice
        force=force,
        version=CLIENT_VERSION,
        home=Path.home(),
        cwd=Path.cwd(),
    )
    result = {
        "target": target,
        "scope": scope,
        "path": str(path),
        "version": CLIENT_VERSION,
        "replaced": previous is not None,
        "replaced_version": previous.version if previous is not None else None,
    }
    emit(console, {"result": result}, ())
