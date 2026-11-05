"""``databay login`` and ``databay logout``."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

import click

from databay.cli.common import common_options, console_for, http_client, login_options
from databay.credentials import TOKEN_STORES
from databay.settings import DEFAULT_PROFILE, DEFAULT_TOKEN_STORE

if TYPE_CHECKING:
    import httpx

    from databay.cli.console import Console
    from databay.credentials import Credential, TokenStore

LOGIN_HELP = f"""Sign in to a Databay server and save it as a profile.

\b
A person signs in in the browser (authorization code with PKCE, back to a
loopback port), or with --device by opening a link on any device. Over SSH
or without a display the device flow is used, and the CLI says so.
\b
An org API key signs in with --api-key-id and its secret from
--api-key-env NAME or --api-key-stdin; never as a flag value.
\b
A person who belongs to several organizations picks the active one with
--org KEY_OR_NAME; without it, the CLI asks on a terminal when none is
active yet, and fails with org_choice_required when it cannot ask.
\b
Tokens go to the OS keyring; --token-store file keeps them in a 0600 file
under the config dir instead. The profile (default: {DEFAULT_PROFILE}) becomes
the current one when there is none yet. Signing in again revokes the
refresh token it replaces."""


def _server(url: str) -> str:
    from databay.errors import DatabayUsageError  # noqa: PLC0415

    if not url.startswith(("https://", "http://")):
        raise DatabayUsageError(f"--server must be an http(s) URL, got {url!r}")
    return url.rstrip("/")


def _api_key_secret(key_id: str | None, env_name: str | None, from_stdin: bool) -> str | None:
    from databay.errors import DatabayUsageError  # noqa: PLC0415

    if key_id is None:
        if env_name or from_stdin:
            raise DatabayUsageError("--api-key-env and --api-key-stdin go with --api-key-id")
        return None
    if bool(env_name) == from_stdin:
        raise DatabayUsageError(
            "--api-key-id needs the key's secret from exactly one of --api-key-env NAME or --api-key-stdin",
            code="secret_required",
        )
    if env_name:
        secret = os.environ.get(env_name, "")
        if not secret:
            raise DatabayUsageError(
                f"--api-key-env names ${env_name}, which is not set or empty", code="secret_required"
            )
        return secret
    secret = sys.stdin.read().strip()
    if not secret:
        raise DatabayUsageError("--api-key-stdin: stdin was empty", code="secret_required")
    return secret


def _previous(store: TokenStore, key: str, console: Console) -> Credential | None:
    """What ``store`` holds under ``key`` before it is overwritten; an unreadable entry is reported and replaced."""
    from databay.errors import DatabayProfileError  # noqa: PLC0415

    try:
        return store.get(key)
    except DatabayProfileError as exc:
        if exc.code == "keyring_unavailable":
            raise
        console.event("credential_replaced", {"store": store.kind, "reason": exc.code})
        return None


def revoke(http: httpx.Client, record: Credential) -> None:
    """Revoke the refresh token of a person's credential at Keycloak; an API key's credential has none."""
    from databay import oidc  # noqa: PLC0415
    from databay.errors import DatabayError  # noqa: PLC0415

    if record.refresh_token is None:
        return
    endpoints = oidc.discover(http, record.issuer)
    if endpoints.revocation is None:
        raise DatabayError(f"{record.issuer} names no revocation endpoint", code="revoke_failed")
    oidc.revoke(http, endpoints.revocation, record.client_id, record.refresh_token)


def _revoke_superseded(http: httpx.Client, records: list[Credential], console: Console) -> int:
    """Revoke refresh tokens a new sign-in replaced; a failure is reported, the sign-in still stands."""
    from databay.errors import DatabayError  # noqa: PLC0415

    revoked = 0
    for record in records:
        try:
            revoke(http, record)
        except DatabayError as exc:
            console.inform(
                "revoke_failed",
                {"sub": record.sub, "error": exc.code},
                f"warning: could not revoke the replaced session ({exc.code}); "
                "ask whoever runs the server to revoke it (docs/cli.md, Sign out)",
            )
            continue
        revoked += record.refresh_token is not None
    return revoked


def _save(name: str, server: str, record: Credential, store: TokenStore, console: Console) -> dict[str, Any]:
    """
    Store ``record`` and point profile ``name`` at it; return what the new sign-in replaced.

    Every profile that names the same credential moves to ``store`` with it. The profile's previous
    credential, when nothing names it any more, is deleted and listed for revocation.
    """
    from databay.credentials import credential_key, open_store  # noqa: PLC0415
    from databay.locks import locked  # noqa: PLC0415
    from databay.profiles import STORED_AUTH, all_profiles, edit_config  # noqa: PLC0415

    key = credential_key(server, record.sub)
    _, profiles = all_profiles()
    replaced: list[Credential] = []
    with locked(key):
        for kind in {p.token_store for p in profiles if p.credential == key} - {store.kind, None}:
            other = open_store(kind)
            previous = _previous(other, key, console)
            if previous is not None:
                replaced.append(previous)
            other.delete(key)
        previous = _previous(store, key, console)
        if previous is not None:
            replaced.append(previous)
        store.put(key, record)

    def change(data: dict[str, Any]) -> tuple[str | None, str | None, bool]:
        entries = data["profiles"]
        old = entries.get(name, {})
        entry = {"server": server, "auth": record.kind, "credential": key, "token_store": store.kind}
        if old.get("server") == server and old.get("active_project"):
            entry["active_project"] = old["active_project"]
        entries[name] = entry
        for other in entries.values():
            if other.get("credential") == key:
                other["token_store"] = store.kind
        became_current = data.get("current_profile") is None
        if became_current:
            data["current_profile"] = name
        dropped = old.get("credential") if old.get("auth") in STORED_AUTH else None
        if dropped == key or any(e.get("credential") == dropped for e in entries.values()):
            dropped = None
        return dropped, old.get("token_store"), became_current or data["current_profile"] == name

    dropped, dropped_store, current = edit_config(change)
    if dropped is not None and dropped_store is not None:
        other = open_store(dropped_store)
        with locked(dropped):
            previous = _previous(other, dropped, console)
            other.delete(dropped)
        if previous is not None:
            replaced.append(previous)
    return {
        "replaced": [r for r in replaced if r.refresh_token is not None and r.refresh_token != record.refresh_token],
        "current": current,
    }


def _choose_org(
    name: str, wanted: str | None, values: dict[str, Any], console: Console, state: dict[str, Any]
) -> str | None:
    """
    The organization the new sign-in acts in: ``wanted``, else the stored one, else the person's choice.

    Runs after the credential is saved, so a refusal here leaves the profile signed in.
    """
    from databay.cli.org import (  # noqa: PLC0415
        choose_interactively,
        interactive,
        list_orgs,
        report_no_usable_org,
        resolve_org,
        switch_to,
    )
    from databay.cli.runner import session_for  # noqa: PLC0415
    from databay.errors import DatabayUsageError  # noqa: PLC0415
    from databay.profiles import load_profile  # noqa: PLC0415
    from databay.stored_token import stored_token  # noqa: PLC0415

    profile = load_profile(name)
    # The credential just saved, even where $DATABAY_TOKEN is set: it is the sign-in this command made.
    session = session_for(profile, stored_token(profile, state.get("http")), values, console, state)
    try:
        listing = list_orgs(session)
        if wanted is not None:
            chosen = resolve_org(listing, wanted, profile.server)
        elif not listing.needs_org_choice:
            if listing.active_org is None and listing.usable_orgs == 0:
                report_no_usable_org(listing, profile.server, console)
            return listing.active_org
        elif interactive(console):
            chosen = choose_interactively(listing, console)
        else:
            raise DatabayUsageError(
                f"signed in to {profile.server}, but you belong to {len(listing.organizations)} organizations "
                "and none is active; pass --org KEY_OR_NAME",
                code="org_choice_required",
                details={"organizations": [o.organization for o in listing.organizations]},
                next_command="databay org list",
            )
        return switch_to(session, profile, chosen, console, state)["organization"]
    finally:
        session.transport.close()


@click.command("login", help=LOGIN_HELP, short_help="Sign in and save a profile")
@click.option("--server", required=True, help="the Databay server URL, e.g. https://databay.example.com")
@click.option("--device", is_flag=True, default=False, help="sign in with a link and code instead of the browser")
@click.option(
    "--token-store",
    type=click.Choice(TOKEN_STORES),
    default=DEFAULT_TOKEN_STORE,
    show_default=True,
    help="where tokens are kept",
)
@click.option("--api-key-id", default=None, metavar="CLIENT_ID", help="sign in as an org API key (apikey-<org>-<id>)")
@click.option("--api-key-env", default=None, metavar="NAME", help="the variable that holds the API key's secret")
@click.option("--api-key-stdin", is_flag=True, default=False, help="read the API key's secret from stdin")
@click.option(
    "--org", "org", default=None, metavar="KEY_OR_NAME", help="the organization to make active after signing in"
)
@login_options
@click.pass_context
def login(
    ctx: click.Context,
    server: str,
    device: bool,
    token_store: str,
    api_key_id: str | None,
    api_key_env: str | None,
    api_key_stdin: bool,
    org: str | None,
    **values: Any,  # noqa: ANN401
) -> None:
    from databay import compat, login_flow, oidc  # noqa: PLC0415
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.credentials import open_store  # noqa: PLC0415
    from databay.errors import DatabayUsageError  # noqa: PLC0415
    from databay.profiles import check_name  # noqa: PLC0415
    from databay.tokens import NoToken  # noqa: PLC0415
    from databay.transport import Transport  # noqa: PLC0415

    console, state = console_for(ctx, values)
    name = check_name(values.get("profile") or DEFAULT_PROFILE)
    server = _server(server)
    state["context"] = {"server": server, "profile": name}
    if device and api_key_id:
        raise DatabayUsageError("--device signs a person in; an API key signs in with --api-key-id alone")
    if org is not None and api_key_id:
        raise DatabayUsageError("--org chooses a person's organization; an API key belongs to one organization")
    secret = _api_key_secret(api_key_id, api_key_env, api_key_stdin)
    store = open_store(token_store)
    store.ready()
    with http_client(state) as http:
        transport = Transport(
            server=server,
            tokens=NoToken(),
            client_kind="cli",
            retries=not values.get("no_retry"),
            verbose=bool(values.get("verbose")),
            events=console.event,
            http=http,
        )
        info, _ = compat.server_info(transport, fresh=True)
        compat.check_server(info)
        endpoints = oidc.discover(http, info.issuer)
        if secret is not None:
            assert api_key_id is not None
            signed = login_flow.api_key_flow(http, endpoints, api_key_id, secret)
            client_id = api_key_id
        else:
            client_id = info.client_id
            reason = (
                None if device else login_flow.device_reason(dict(os.environ), sys.platform, login_flow.local_browser)
            )
            if reason is not None:
                console.inform(
                    "device_login_chosen",
                    {"reason": reason},
                    f"{reason[0].upper()}{reason[1:]}; using device login. Pass --device to choose it directly.",
                )
            flow = login_flow.device_flow if device or reason else login_flow.browser_flow
            signed = flow(http, endpoints, client_id, console)
        record = login_flow.credential(server, info, signed, client_id, secret)
        saved = _save(name, server, record, store, console)
        revoked = _revoke_superseded(http, saved["replaced"], console)
    chosen_org = None if secret is not None else _choose_org(name, org, values, console, state)
    claims = oidc.claims(record.access_token)
    result = {
        "profile": name,
        "server": server,
        "auth": record.kind,
        "user": claims.get("preferred_username"),
        "sub": record.sub,
        "token_store": store.kind,
        "current": saved["current"],
        "revoked_superseded": revoked,
        "org": chosen_org,
    }
    next_command = "databay status" if saved["current"] else f"databay profile use {name}"
    emit(console, {"result": result, "next": next_command}, ())


@click.command("logout", short_help="Sign out: revoke and delete the profile's credential")
@common_options
@click.pass_context
def logout(ctx: click.Context, **values: Any) -> None:  # noqa: ANN401
    """
    Revoke the profile's refresh token at Keycloak, then delete its stored credential.

    Every profile that named the credential loses it. When the revocation fails the credential is
    still deleted here, and the command exits 1 with revoke_failed.
    """
    from databay.cli.output import emit  # noqa: PLC0415
    from databay.credentials import open_store  # noqa: PLC0415
    from databay.errors import DatabayError, DatabayUsageError  # noqa: PLC0415
    from databay.locks import locked  # noqa: PLC0415
    from databay.profiles import edit_config, load_profile  # noqa: PLC0415

    console, state = console_for(ctx, values)
    profile = load_profile(values.get("profile"))
    state["context"] = {"server": profile.server, "profile": profile.name}
    if profile.auth == "env":
        raise DatabayUsageError(f"profile {profile.name} reads $DATABAY_TOKEN and stores nothing to sign out of")
    key = profile.credential
    if key is None or profile.token_store is None:
        emit(console, {"result": {"credential": None, "profiles": [], "revoked": False}}, ())
        return
    store = open_store(profile.token_store)
    failure: DatabayError | None = None
    with locked(key):
        record = _previous(store, key, console)
        if record is not None:
            with http_client(state) as http:
                try:
                    revoke(http, record)
                except DatabayError as exc:
                    failure = exc
        store.delete(key)

    def change(data: dict[str, Any]) -> list[str]:
        cleared = []
        for other_name, entry in data["profiles"].items():
            if entry.get("credential") == key:
                entry.pop("credential")
                entry.pop("token_store", None)
                cleared.append(other_name)
        return sorted(cleared)

    cleared = edit_config(change)
    revoked = failure is None and record is not None and record.refresh_token is not None
    emit(console, {"result": {"credential": key, "profiles": cleared, "revoked": revoked}}, ())
    if failure is not None:
        raise DatabayError(
            f"signed out locally, but Keycloak refused to revoke the session: {failure.message}",
            code="revoke_failed",
            details={"cause": failure.code, "profiles": cleared},
        )
