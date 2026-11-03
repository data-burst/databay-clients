"""
Profiles: which server a command talks to, how it gets its token, and its active project.

``config.toml`` in the config dir::

    current_profile = "staging"

    [profiles.staging]
    server = "https://databay.example.com"
    auth = "login"                     # login, api_key or env
    credential = "databay-cli:https://databay.example.com:<sub>"
    token_store = "keyring"            # keyring or file
    active_project = "<project id>"    # optional

The profile alone picks the token: ``login`` and ``api_key`` read the stored ``credential``
(written by ``databay login``), ``env`` reads ``$DATABAY_TOKEN``. A command uses ``--profile NAME``,
else ``current_profile``; with neither it fails with ``no_profile``. A key this version does not
know is an error, never ignored. Writers hold the config lock and replace the file whole.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from databay.errors import DatabayProfileError, DatabayUsageError
from databay.locks import CONFIG_LOCK, locked
from databay.paths import config_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PROFILE_KEYS = frozenset({"server", "auth", "credential", "token_store", "active_project"})
TOP_LEVEL_KEYS = frozenset({"current_profile", "profiles"})
#: The order a written profile lists its keys in.
KEY_ORDER = ("server", "auth", "credential", "token_store", "active_project")
#: The token sources that read a stored credential.
STORED_AUTH = frozenset({"login", "api_key"})
PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    server: str
    auth: str
    active_project: str | None = None
    credential: str | None = None
    token_store: str | None = None


def _invalid(path: Path, problem: str) -> DatabayProfileError:
    return DatabayProfileError(f"{path}: {problem}", code="profile_invalid")


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(path, f"not valid TOML ({exc})") from exc
    unknown = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown:
        raise _invalid(path, f"unknown keys {unknown}; allowed: {sorted(TOP_LEVEL_KEYS)}")
    if not isinstance(data.get("profiles", {}), dict):
        raise _invalid(path, "profiles is not a table")
    return data


def _optional_text(path: Path, name: str, entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise _invalid(path, f"profiles.{name}.{key} must be a non-empty string")
    return value


def _profile(path: Path, name: str, entry: Any) -> Profile:  # noqa: ANN401 — a parsed TOML value
    from databay.credentials import TOKEN_STORES  # noqa: PLC0415
    from databay.tokens import TOKEN_SOURCES  # noqa: PLC0415 — tokens imports this module's types

    if not isinstance(entry, dict):
        raise _invalid(path, f"profiles.{name} is not a table")
    unknown = sorted(set(entry) - PROFILE_KEYS)
    if unknown:
        raise _invalid(path, f"profiles.{name} has unknown keys {unknown}; allowed: {sorted(PROFILE_KEYS)}")
    server = entry.get("server")
    if not isinstance(server, str) or not server.startswith(("https://", "http://")):
        raise _invalid(path, f"profiles.{name}.server must be an http(s) URL, got {server!r}")
    auth = entry.get("auth")
    if auth not in TOKEN_SOURCES:
        raise _invalid(path, f"profiles.{name}.auth must be one of {sorted(TOKEN_SOURCES)}, got {auth!r}")
    project = _optional_text(path, name, entry, "active_project")
    credential = _optional_text(path, name, entry, "credential")
    store = _optional_text(path, name, entry, "token_store")
    if auth not in STORED_AUTH and (credential or store):
        raise _invalid(
            path, f"profiles.{name} has auth = {auth!r}, which stores nothing: remove credential and token_store"
        )
    if (credential is not None or store is not None) and store not in TOKEN_STORES:
        raise _invalid(path, f"profiles.{name}.token_store must be one of {list(TOKEN_STORES)}, got {store!r}")
    return Profile(
        name=name,
        server=server.rstrip("/"),
        auth=auth,
        active_project=project,
        credential=credential,
        token_store=store,
    )


def load_profile(name: str | None) -> Profile:
    """The profile ``name``, else the config's ``current_profile``; ``no_profile`` when there is neither."""
    path = config_file()
    data = _read(path)
    chosen = name or data.get("current_profile")
    if chosen is None:
        raise DatabayProfileError(
            f"no profile: pass --profile NAME or sign in with databay login --server URL (config: {path})"
        )
    profiles = data.get("profiles", {})
    if chosen not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        raise DatabayProfileError(f"no profile {chosen!r} in {path} (profiles: {known})")
    return _profile(path, chosen, profiles[chosen])


def all_profiles() -> tuple[str | None, list[Profile]]:
    """``current_profile`` and every profile, by name."""
    path = config_file()
    data = _read(path)
    profiles = data.get("profiles", {})
    return data.get("current_profile"), [_profile(path, name, profiles[name]) for name in sorted(profiles)]


def _toml(data: dict[str, Any]) -> str:
    """``config.toml`` text: strings only, every value JSON-quoted (a JSON string is a TOML basic string)."""
    lines = []
    if data.get("current_profile") is not None:
        lines += [f"current_profile = {json.dumps(data['current_profile'])}", ""]
    for name, entry in sorted(data.get("profiles", {}).items()):
        lines.append(f"[profiles.{json.dumps(name)}]")
        lines += [f"{key} = {json.dumps(entry[key])}" for key in KEY_ORDER if entry.get(key) is not None]
        lines.append("")
    return "\n".join(lines)


def edit_config(change: Callable[[dict[str, Any]], Any]) -> Any:  # noqa: ANN401 — what ``change`` returns
    """
    Apply ``change`` to the parsed config under the config lock, then write it; returns what ``change`` returns.

    The result is validated before it is written, so a change never leaves a config this version cannot read.
    """
    path = config_file()
    with locked(CONFIG_LOCK):
        data = _read(path)
        data.setdefault("profiles", {})
        outcome = change(data)
        for name, entry in data["profiles"].items():
            _profile(path, name, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(_toml(data), encoding="utf-8")
        tmp.replace(path)
    return outcome


def login_command(profile: Profile) -> str:
    """The command that signs ``profile`` in again."""
    return f"databay login --server {profile.server} --profile {profile.name}"


def check_name(name: str) -> str:
    if not PROFILE_NAME.match(name):
        raise DatabayUsageError(f"profile name {name!r}: use 1 to 64 letters, digits, - or _")
    return name
