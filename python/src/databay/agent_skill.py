"""
The agent skill that ships in ``databay/skill/``: where each agent loads it, and installing it there.

The installed ``SKILL.md`` carries the CLI version in its frontmatter (``metadata``), so
``databay status`` can tell a copy from another CLI version. Standard library only.
"""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from databay.errors import DatabayError, DatabayUsageError

Scope = Literal["user", "project"]
SCOPES: tuple[Scope, ...] = ("user", "project")

#: Each supported agent's skills folder, under the home (user scope) or the working directory
#: (project scope). Codex reads ``~/.agents/skills`` and ``.agents/skills`` from the working
#: directory up to the project root (openai/codex ``codex-rs/ext/skills/src/host_roots.rs``).
TARGETS: dict[str, Path] = {"claude": Path(".claude") / "skills", "codex": Path(".agents") / "skills"}

SKILL_NAME = "databay"
VERSION_KEY = "databay-cli-version"
SOURCE = Path(__file__).resolve().parent / "skill"
_VERSION_LINE = re.compile(rf"^\s+{re.escape(VERSION_KEY)}:\s*\"?([^\"\s]+)\"?\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Installed:
    """
    A skill folder found at ``path``.

    ``version`` is the CLI version its ``SKILL.md`` records; when it is ``None``, ``problem`` says
    why (no ``SKILL.md``, unreadable, no frontmatter, or no version in it).
    """

    target: str
    scope: Scope
    path: Path
    version: str | None
    problem: str | None


def skill_dir(target: str, scope: Scope, *, home: Path, cwd: Path) -> Path:
    """The folder ``target`` loads the skill from; an unknown target is ``unsupported_target``."""
    if target not in TARGETS:
        raise DatabayUsageError(
            f"no skill format for target {target!r}; supported targets: {', '.join(TARGETS)}",
            code="unsupported_target",
            details={"target": target},
        )
    base = home if scope == "user" else cwd
    return base / TARGETS[target] / SKILL_NAME


def _present(path: Path) -> bool:
    """Something is at ``path``: a folder, a file or a symlink, broken or not."""
    return path.exists() or path.is_symlink()


def _frontmatter_end(text: str) -> int | None:
    """The index of the closing ``---`` line of ``text``'s frontmatter, or ``None`` when it has none."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    return end if end >= 0 else None


def rendered_skill(version: str) -> str:
    """The packaged ``SKILL.md`` with ``version`` added to its frontmatter as ``metadata.databay-cli-version``."""
    path = SOURCE / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    end = _frontmatter_end(text)
    if end is None:
        raise DatabayError(f"{path} has no --- frontmatter")
    if re.search(r"^metadata:", text[:end], re.MULTILINE):
        raise DatabayError(f"{path} declares metadata itself; the installer writes that block")
    return f'{text[:end]}\nmetadata:\n  {VERSION_KEY}: "{version}"{text[end:]}'


def read_installed(target: str, scope: Scope, path: Path) -> Installed:
    """The copy at ``path``: the version its ``SKILL.md`` records, or the reason none could be read."""
    skill = path / "SKILL.md"
    try:
        # CRLF is accepted: an editor on another platform may have rewritten the file.
        text = skill.read_text(encoding="utf-8").replace("\r\n", "\n")
    except FileNotFoundError:
        return Installed(target, scope, path, None, f"{skill} does not exist")
    except (OSError, UnicodeDecodeError) as exc:
        return Installed(target, scope, path, None, f"{skill} cannot be read ({type(exc).__name__})")
    end = _frontmatter_end(text)
    if end is None:
        return Installed(target, scope, path, None, f"{skill} has no --- frontmatter")
    found = _VERSION_LINE.search(text[:end])
    if found is None:
        return Installed(target, scope, path, None, f"{skill} records no metadata.{VERSION_KEY}")
    return Installed(target, scope, path, found.group(1), None)


def project_dirs(*, home: Path, cwd: Path) -> list[Path]:
    """
    ``cwd`` and its parents, where a project-scope copy may sit, nearest first.

    The walk stops at the home: a copy there is the user-scope one.
    """
    found = []
    for directory in [cwd, *cwd.parents]:
        if directory == home or directory in home.parents:
            break
        found.append(directory)
    return found


def find_installed(*, home: Path, cwd: Path) -> list[Installed]:
    """Every installed copy: per target, the user one, then project ones from ``cwd`` up."""
    bases: list[tuple[Scope, Path]] = [("user", home), *[("project", d) for d in project_dirs(home=home, cwd=cwd)]]
    found = []
    for target, folder in TARGETS.items():
        for scope, base in bases:
            path = base / folder / SKILL_NAME
            if _present(path):
                found.append(read_installed(target, scope, path))
    return found


def install(
    target: str, scope: Scope, *, force: bool, version: str, home: Path, cwd: Path
) -> tuple[Path, Installed | None]:
    """
    Write the skill (``SKILL.md`` and ``references/``) where ``target`` loads it.

    Returns the folder it wrote and the copy it replaced, if any. A copy from another version
    (or of no readable version) is replaced only with ``force``; otherwise it is ``skill_exists``.
    The new copy is staged beside the old one and swapped in; a failed swap puts the old one back.
    """
    destination = skill_dir(target, scope, home=home, cwd=cwd)
    previous = read_installed(target, scope, destination) if _present(destination) else None
    if previous is not None and previous.version != version and not force:
        installed = previous.version or f"an unknown version ({previous.problem})"
        raise DatabayUsageError(
            f"{destination} holds the databay skill of CLI {installed}; this CLI is {version}",
            code="skill_exists",
            next_command=f"databay skill install --target {target} --scope {scope} --force",
            details={"path": str(destination), "installed_version": previous.version, "cli_version": version},
        )
    staging = destination.parent / f".{SKILL_NAME}-new-{uuid.uuid4().hex}"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SOURCE / "references", staging / "references")
        (staging / "SKILL.md").write_text(rendered_skill(version), encoding="utf-8")
        _swap(staging, destination, has_previous=previous is not None)
    except OSError as exc:
        raise DatabayError(
            f"could not write the skill to {destination}: {type(exc).__name__}: {exc.strerror or exc}",
            code="skill_write_failed",
            details={"path": str(destination)},
        ) from None
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination, previous


def _swap(staging: Path, destination: Path, *, has_previous: bool) -> None:
    """Move ``staging`` to ``destination``; the copy already there is moved aside first and removed after."""
    if not has_previous:
        staging.rename(destination)
        return
    aside = destination.parent / f".{SKILL_NAME}-old-{uuid.uuid4().hex}"
    destination.rename(aside)
    try:
        staging.rename(destination)
    except OSError:
        aside.rename(destination)
        raise
    if aside.is_symlink() or not aside.is_dir():
        aside.unlink()
        return
    shutil.rmtree(aside)
