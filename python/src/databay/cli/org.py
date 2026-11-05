"""
``databay org switch``, and the organization ``databay login`` makes active.

The server stores the active organization and every token names that one. Switching is a write
(``PUT /api/auth/me/active-org``) followed by a token renewal; the new token is checked to name the
organization before the command reports it. An organization is named by its key or its display name.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import click

from databay.cli.common import common_options, console_for
from databay.cli.handwritten import HANDWRITTEN

if TYPE_CHECKING:
    from databay.cli.console import Console
    from databay.invoke import Session
    from databay.models.org import MyOrganizationOut, MyOrganizationsResponse
    from databay.profiles import Profile

SWITCH_HELP = """Make ORGANIZATION the active organization and refresh the token.

\b
ORGANIZATION is a key (org-...) or a display name; `databay org list` shows
both. Every later command acts in that organization. The profile's active
project belongs to the previous organization, so it is cleared.

\b
Example:
  databay org switch <organization>"""


def list_orgs(session: Session) -> MyOrganizationsResponse:
    from databay.invoke import CallValues  # noqa: PLC0415
    from databay.operations.org import ORG_LIST  # noqa: PLC0415

    return session.call(ORG_LIST, CallValues()).parsed


def resolve_org(listing: MyOrganizationsResponse, wanted: str, server: str) -> MyOrganizationOut:
    """The organization ``wanted`` names: a key, else exactly one display name, compared without case."""
    from databay.errors import DatabayUsageError, api_error  # noqa: PLC0415

    keys = [org.organization for org in listing.organizations]
    by_key = [org for org in listing.organizations if org.organization == wanted]
    if by_key:
        return by_key[0]
    by_name = [org for org in listing.organizations if org.display_name.casefold() == wanted.casefold()]
    if len(by_name) > 1:
        raise DatabayUsageError(
            f"{len(by_name)} of your organizations are named {wanted!r}; pass the key instead",
            details={"organizations": [org.organization for org in by_name]},
        )
    if not by_name:
        raise api_error(
            "org_not_member",
            f"you belong to no organization {wanted!r} on {server}",
            status=403,
            details={"organization": wanted, "organizations": keys},
        )
    return by_name[0]


def switch_to(
    session: Session, profile: Profile, org: MyOrganizationOut, console: Console, state: dict[str, Any]
) -> dict[str, Any]:
    """
    Store ``org`` as active, renew the profile's token and check it names ``org``.

    A profile whose token comes from ``$DATABAY_TOKEN`` cannot renew it: the switch is stored and
    ``token_refreshed`` is false. The active project is cleared when the token named another organization.
    """
    from databay.errors import DatabayError  # noqa: PLC0415
    from databay.invoke import CallValues  # noqa: PLC0415
    from databay.operations.org import ORG_SWITCH  # noqa: PLC0415
    from databay.profiles import edit_config  # noqa: PLC0415
    from databay.settings import ENV_TOKEN  # noqa: PLC0415
    from databay.stored_token import StoredToken  # noqa: PLC0415

    before = _token_org(session)
    state["sent"] = True
    session.call(ORG_SWITCH, CallValues(body={"organization": org.organization}, has_body=True))
    tokens = session.transport.tokens
    refreshed = isinstance(tokens, StoredToken)
    if refreshed:
        tokens.renewed()
        now = _token_org(session)
        if now != org.organization:
            raise DatabayError(
                f"switched to {org.organization}, but the renewed token names {now or 'no organization'}",
                code="org_not_in_token",
                details={"organization": org.organization, "token_organization": now},
                next_command="databay status",
            )
    else:
        console.inform(
            "token_not_refreshed",
            {"organization": org.organization, "auth": profile.auth},
            f"{org.organization} is now your active organization, but ${ENV_TOKEN} still holds a token "
            "for the previous one: get a new token before the next command.",
        )
    cleared = None
    if before != org.organization and profile.active_project is not None:
        cleared = profile.active_project

        def change(data: dict[str, Any]) -> None:
            data["profiles"][profile.name].pop("active_project", None)

        edit_config(change)
    return {
        "profile": profile.name,
        "organization": org.organization,
        "display_name": org.display_name,
        "role": None if org.role is None else str(org.role),
        "token_refreshed": refreshed,
        "cleared_active_project": cleared,
    }


def _token_org(session: Session) -> str | None:
    """The organization the token now names, as the server reads it."""
    from databay.invoke import CallValues  # noqa: PLC0415
    from databay.operations.me import ME_GET  # noqa: PLC0415

    me = session.call(ME_GET, CallValues()).parsed
    return me.organization[0] if me.organization else None


def choose_interactively(listing: MyOrganizationsResponse, console: Console) -> MyOrganizationOut:
    """Ask on the terminal which organization to make active."""
    lines = ["You belong to several organizations. Which one should be active?"]
    for number, org in enumerate(listing.organizations, start=1):
        role = f", {org.role}" if org.role is not None else ""
        lines.append(f"  {number}) {org.display_name} ({org.organization}{role})")
    console.inform("org_choice", {}, "\n".join(lines))
    picked = click.prompt("Organization", type=click.IntRange(1, len(listing.organizations)), err=True)
    return listing.organizations[picked - 1]


def interactive(console: Console) -> bool:
    return sys.stdin.isatty() and console.stderr_tty


@click.command("switch", help=SWITCH_HELP, short_help=HANDWRITTEN["org switch"][2])
@click.argument("organization")
@common_options
@click.pass_context
def org_switch(ctx: click.Context, organization: str, **values: Any) -> None:  # noqa: ANN401
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.cli.runner import open_session  # noqa: PLC0415
    from databay.errors import DatabayUsageError  # noqa: PLC0415

    console, state = console_for(ctx, values)
    session, chosen = open_session(values, console, state)
    state["context"] = {"server": chosen.server, "profile": chosen.name}
    if chosen.auth == "api_key":
        raise DatabayUsageError(f"profile {chosen.name} signs in as an org API key, which belongs to one organization")
    try:
        org = resolve_org(list_orgs(session), organization, chosen.server)
        result = switch_to(session, chosen, org, console, state)
    finally:
        session.transport.close()
    emit(console, {"result": result, "next": "databay status"}, ())
