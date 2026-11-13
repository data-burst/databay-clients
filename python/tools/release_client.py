"""
The steps of a ``databay-cli`` release that semantic-release cannot do itself; see docs/cli.md.

    python python/tools/release_client.py ready                    # may main be released now?
    python python/tools/release_client.py stamp 0.2.0              # write the version semantic-release picked
    python python/tools/release_client.py check --tag python/v0.2.0

``ready`` refuses a ``contract/`` that is not exactly one release synced from the server: the released
spec equals ``core.json``, its meta matches the API version and error codes, and every file matches the
digest ``contract/SOURCE`` records. The release workflow runs semantic-release only when it passes.
``stamp`` is semantic-release's build command: it sets the version in ``pyproject.toml`` and ``uv.lock``
and regenerates the client. ``check`` is what the workflow runs on the tag before building.

Standard library only: the workflow runs this before installing anything.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contract import (
    API_VERSION_FILE,
    CONTRACT,
    ERROR_CODES,
    RELEASED_SPEC,
    REPO_ROOT,
    SOURCE,
    SPEC,
    ContractError,
    api_version_in,
    load_released,
    load_source,
    registry_codes,
    source_mismatches,
)

CLIENT_ROOT = REPO_ROOT / "python"
PYPROJECT = CLIENT_ROOT / "pyproject.toml"
CLIENT_LOCK = CLIENT_ROOT / "uv.lock"
CHANGELOG = CLIENT_ROOT / "CHANGELOG.md"
GENERATOR = CLIENT_ROOT / "tools" / "gen_client.py"
PACKAGE = "databay-cli"
TAG_PREFIX = "python/v"


class ReleaseRefused(Exception):
    """A release that must not go ahead; the message says why and what to do."""


def parse_version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ReleaseRefused(f"{value!r} is not a version: use major.minor.patch, like 0.2.0")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def project_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, cwd=REPO_ROOT)  # noqa: S603


def _generated_files_differ() -> str | None:
    done = _run(sys.executable, str(GENERATOR), "--check")
    return None if done.returncode == 0 else (done.stdout + done.stderr).strip()


def _replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ReleaseRefused(f"{path.relative_to(REPO_ROOT)}: expected one match of {pattern!r}, found {count}")
    path.write_text(updated, encoding="utf-8")


def _set_version(version: str) -> None:
    _replace_once(PYPROJECT, r'^version = "[^"]+"$', f'version = "{version}"')
    # Edited in place: a version is all that changes, and locking again could move dependencies.
    _replace_once(CLIENT_LOCK, rf'^(name = "{PACKAGE}"\nversion = )"[^"]+"$', rf'\g<1>"{version}"')


def lock_version() -> str:
    """The version ``uv.lock`` records for this package."""
    found = re.findall(
        rf'^name = "{PACKAGE}"\nversion = "([^"]+)"$', CLIENT_LOCK.read_text(encoding="utf-8"), re.MULTILINE
    )
    if len(found) != 1:
        raise ReleaseRefused(f"{CLIENT_LOCK.name}: expected one {PACKAGE} entry, found {len(found)}")
    return found[0]


def refuse_unless_one_release() -> None:
    """Refuse a ``contract/`` that is not one release exactly as the server's release sync wrote it."""
    differ = source_mismatches(CONTRACT, load_source(SOURCE))
    if differ:
        raise ReleaseRefused(
            "contract/ differs from contract/SOURCE; it changes only through the server's sync:\n  "
            + "\n  ".join(differ)
        )
    if not RELEASED_SPEC.exists():
        raise ReleaseRefused(
            f"contract/{RELEASED_SPEC.name} does not exist: the server has not cut a release; "
            "a release needs a release sync of a cut"
        )
    if RELEASED_SPEC.read_bytes() != SPEC.read_bytes():
        raise ReleaseRefused(
            f"contract/{RELEASED_SPEC.name} differs from {SPEC.name}: this contract is not one release; "
            "a release needs a release sync of a cut"
        )
    released = load_released(RELEASED_SPEC)
    if released is None:
        raise ReleaseRefused(f"contract/{RELEASED_SPEC.name} could not be read")
    api_version = api_version_in(API_VERSION_FILE)
    if released.api_version != api_version:
        raise ReleaseRefused(
            f"the released contract records API {released.api_version}, but api_version.json is {api_version}"
        )
    registry = json.loads(ERROR_CODES.read_text(encoding="utf-8"))
    if (released.error_codes, released.cdc_error_codes) != registry_codes(registry):
        raise ReleaseRefused(f"the released contract's error codes differ from {ERROR_CODES.name}")


def _regenerate() -> None:
    done = _run(sys.executable, str(GENERATOR))
    if done.returncode != 0:
        raise ReleaseRefused(f"python/tools/gen_client.py failed:\n{done.stdout}{done.stderr}")


def stamp(version: str) -> None:
    """Write ``version`` everywhere the package records it."""
    parse_version(version)
    _set_version(version)
    _regenerate()


def check(tag: str) -> None:
    """Refuse a tag whose commit is not exactly the release it names."""
    if not tag.startswith(TAG_PREFIX):
        raise ReleaseRefused(f"tag {tag!r} does not start with {TAG_PREFIX}")
    version = tag.removeprefix(TAG_PREFIX)
    parse_version(version)
    current = project_version()
    if version != current:
        raise ReleaseRefused(f"tag {tag} names {version}, but {PYPROJECT.name} is at {current}")
    locked = lock_version()
    if locked != current:
        raise ReleaseRefused(f"{CLIENT_LOCK.name} records {PACKAGE} {locked}, but {PYPROJECT.name} is at {current}")
    refuse_unless_one_release()
    differ = _generated_files_differ()
    if differ is not None:
        raise ReleaseRefused(f"the generated client does not match this commit:\n{differ}")
    if not re.search(
        rf"^## v{re.escape(version)} \(\d{{4}}-\d{{2}}-\d{{2}}\)$", CHANGELOG.read_text("utf-8"), re.MULTILINE
    ):
        raise ReleaseRefused(f"{CHANGELOG.name} has no section for {version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ready", help="refuse a contract/ that is not one release")
    commands.add_parser("stamp", help="write a version into the package").add_argument("version")
    commands.add_parser("check", help="refuse a tag that is not this commit's release").add_argument(
        "--tag", required=True
    )
    args = parser.parse_args()
    try:
        if args.command == "ready":
            refuse_unless_one_release()
            print("contract/ is one release")
        elif args.command == "stamp":
            stamp(args.version)
            print(f"stamped {PACKAGE} {args.version}")
        else:
            check(args.tag)
            print(f"{args.tag} matches this commit")
    except (ReleaseRefused, ContractError) as exc:
        raise SystemExit(f"release_client: {exc}") from exc


if __name__ == "__main__":
    main()
