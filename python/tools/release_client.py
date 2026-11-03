"""
Prepare a ``databay-cli`` release, and check that a tagged commit is one.

    python python/tools/release_client.py prepare 0.2.0       # commit the release on a new branch
    python python/tools/release_client.py check --tag python/v0.2.0

``prepare`` moves the version, dates the changelog's Unreleased section and regenerates the client. It
commits that on ``release/databay-cli-v<version>`` for a PR that merges before the tag, and never
pushes, tags or publishes. ``check`` is what the release workflow runs on the tag; see docs/cli.md.

Both refuse a ``contract/`` that is not exactly one release synced from the server: the released spec
equals ``core.json``, its meta matches the API version and error codes, and every file matches the
digest ``contract/SOURCE`` records.

Standard library only: the workflow runs this before installing anything.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
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
UNRELEASED_HEADING = "## Unreleased"


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


def _git(*args: str) -> str:
    done = _run("git", *args)
    if done.returncode != 0:
        raise ReleaseRefused(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


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


def _unreleased_notes(text: str) -> str:
    if text.count(f"{UNRELEASED_HEADING}\n") != 1:
        raise ReleaseRefused(f"{CHANGELOG.name}: expected one '{UNRELEASED_HEADING}' heading")
    _, _, rest = text.partition(f"{UNRELEASED_HEADING}\n")
    return re.split(r"^## ", rest, maxsplit=1, flags=re.MULTILINE)[0].strip()


def _date_changelog(version: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    today = datetime.now(UTC).date().isoformat()
    CHANGELOG.write_text(
        text.replace(f"{UNRELEASED_HEADING}\n", f"{UNRELEASED_HEADING}\n\n## {version} - {today}\n", 1),
        encoding="utf-8",
    )


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


def _refuse_unless_current_main() -> None:
    """The tag goes on the merge of this branch, so the release starts from what main holds now."""
    _git("fetch", "--quiet", "origin", "main")
    head = _git("rev-parse", "HEAD").strip()
    main = _git("rev-parse", "origin/main").strip()
    if head != main:
        raise ReleaseRefused(f"HEAD is {head}, not origin/main ({main}); switch to main and pull first")


def _write_release(version: str) -> None:
    _set_version(version)
    _date_changelog(version)
    _regenerate()
    check(f"{TAG_PREFIX}{version}")
    _git("add", "-A", "--", "python")
    _git("commit", "-m", f"chore(release): databay-cli {version}")


def _roll_back(start: list[str], branch: str) -> None:
    """Back to where ``prepare`` started. The tree was clean then, so this removes only what ``prepare`` wrote."""
    _git("reset", "--hard", "--quiet")
    _git("clean", "-fdq", "--", "python")
    _git("switch", "--quiet", *start)
    _git("branch", "-D", "--quiet", branch)


def _regenerate() -> None:
    done = _run(sys.executable, str(GENERATOR))
    if done.returncode != 0:
        raise ReleaseRefused(f"python/tools/gen_client.py failed:\n{done.stdout}{done.stderr}")


def prepare(version: str) -> str:
    """Commit the release on a new branch and return the branch's name."""
    new = parse_version(version)
    current = project_version()
    if new <= parse_version(current):
        raise ReleaseRefused(f"version {version} does not go up: {PYPROJECT.name} is at {current}")
    dirty = _git("status", "--porcelain")
    if dirty.strip():
        raise ReleaseRefused(
            f"the working tree has uncommitted changes; commit or remove them first:\n{dirty.rstrip()}"
        )
    _git("var", "GIT_AUTHOR_IDENT")
    _git("var", "GIT_COMMITTER_IDENT")
    _refuse_unless_current_main()
    refuse_unless_one_release()
    differ = _generated_files_differ()
    if differ is not None:
        raise ReleaseRefused(f"the generated client is stale; run python python/tools/gen_client.py first:\n{differ}")
    if not _unreleased_notes(CHANGELOG.read_text(encoding="utf-8")):
        raise ReleaseRefused(f"{CHANGELOG.name}: '{UNRELEASED_HEADING}' is empty; say what this release changes")

    branch = f"release/databay-cli-v{version}"
    current_branch = _git("branch", "--show-current").strip()
    start = [current_branch] if current_branch else ["--detach", _git("rev-parse", "HEAD").strip()]
    _git("switch", "--quiet", "-c", branch)
    try:
        _write_release(version)
    except Exception as exc:
        try:
            _roll_back(start, branch)
        except ReleaseRefused as failed:
            raise ReleaseRefused(
                f"{exc}\nthen rolling back failed, so the repository is not as it was: {failed}"
            ) from exc
        raise
    return branch


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
        rf"^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}$", CHANGELOG.read_text("utf-8"), re.MULTILINE
    ):
        raise ReleaseRefused(f"{CHANGELOG.name} has no section for {version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="commit a release on a new branch").add_argument("version")
    commands.add_parser("check", help="refuse a tag that is not this commit's release").add_argument(
        "--tag", required=True
    )
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            branch = prepare(args.version)
            print(
                f"committed the release on {branch}. Next: push it and open a PR. After it merges, an owner tags "
                f"the merge commit on main with {TAG_PREFIX}{args.version}; the tag publishes to PyPI."
            )
        else:
            check(args.tag)
            print(f"{args.tag} matches this commit")
    except (ReleaseRefused, ContractError) as exc:
        raise SystemExit(f"release_client: {exc}") from exc


if __name__ == "__main__":
    main()
