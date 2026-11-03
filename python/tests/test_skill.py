"""The agent skill: what ``SKILL.md`` names exists, ``databay skill install`` and the skill check in ``status``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cli_parse import every_option_name, parse
from conftest import answer

from databay.agent_skill import SOURCE, read_installed
from databay.cli.handwritten import HANDWRITTEN
from databay.version import CLIENT_VERSION

SKILL = SOURCE / "SKILL.md"
ME = {"sub": "u-1", "preferred_username": "ada", "organization": ["acme"], "principal": "user"}


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _command_lines() -> list[str]:
    """Every ``databay …`` line of a code block, and every inline ``databay …`` span, except the ``<noun>`` form."""
    text = _skill_text()
    lines = [line.strip() for block in re.findall(r"```\n(.*?)```", text, re.DOTALL) for line in block.splitlines()]
    found = [line for line in lines if line.startswith("databay ")]
    found += re.findall(r"`(databay [^`]+)`", text)
    return [line for line in found if "<noun>" not in line]


def _named_flags() -> set[str]:
    """Every inline span that starts with a flag, by its first word; ``--<field>-env`` patterns are left out."""
    return {span.split()[0] for span in re.findall(r"`(--[^`]+)`", _skill_text()) if "<" not in span.split()[0]}


def _frontmatter(text: str) -> dict[str, str]:
    """The frontmatter's top-level keys and the ``metadata`` entries, as written (a folded scalar is joined)."""
    block = text.split("---\n", 2)[1]
    found: dict[str, str] = {}
    key = None
    for line in block.splitlines():
        if not line.startswith(" "):
            key, _, value = line.partition(":")
            found[key] = value.strip()
        elif key == "metadata":
            name, _, value = line.strip().partition(":")
            found[f"metadata.{name}"] = value.strip().strip('"')
        else:
            found[key] = f"{found[key]} {line.strip()}".strip()
    return found


@pytest.mark.parametrize("line", _command_lines())
def test_every_command_the_skill_names_parses_with_the_real_cli(line):
    ctx = parse(line)

    assert ctx.command.callback is not None, f"{line} stops at a group"


def test_every_hand_written_command_is_in_the_commands_reference():
    reference = (SOURCE / "references" / "commands.md").read_text(encoding="utf-8")

    assert [name for name in HANDWRITTEN if f"`databay {name}`" not in reference] == []


def test_every_flag_the_skill_names_exists_in_the_cli():
    named = _named_flags()

    assert named, "the skill names no flag"
    assert named - every_option_name() == set()


def test_the_packaged_skill_has_what_both_agents_require_and_no_version():
    front = _frontmatter(_skill_text())

    assert front["name"] == "databay"
    assert len(front["description"]) > 100
    assert "metadata" not in front


INSTALL_PATHS = [
    ("claude", "user", "home", ".claude/skills/databay"),
    ("claude", "project", "work", ".claude/skills/databay"),
    ("codex", "user", "home", ".agents/skills/databay"),
    ("codex", "project", "work", ".agents/skills/databay"),
]


@pytest.mark.parametrize(("target", "scope", "base", "relative"), INSTALL_PATHS)
def test_install_writes_the_skill_and_its_references_where_the_agent_loads_it(
    cli, profile_env, target, scope, base, relative
):
    run = cli("skill", "install", "--target", target, "--scope", scope)

    assert run.code == 0, run.stderr
    folder = profile_env[base] / relative
    assert run.json()["result"]["path"] == str(folder)
    for reference in ("commands.md", "errors.md"):
        packaged = (SOURCE / "references" / reference).read_bytes()
        assert (folder / "references" / reference).read_bytes() == packaged
    other = profile_env["work" if base == "home" else "home"]
    assert not any(other.rglob("SKILL.md"))


def test_install_defaults_to_the_user_scope(cli, profile_env):
    run = cli("skill", "install", "--target", "claude")

    assert run.code == 0, run.stderr
    assert (profile_env["home"] / ".claude/skills/databay/SKILL.md").is_file()


def test_the_installed_skill_records_the_cli_version_in_its_frontmatter(cli, profile_env):
    cli("skill", "install", "--target", "codex")

    installed = (profile_env["home"] / ".agents/skills/databay/SKILL.md").read_text(encoding="utf-8")
    front = _frontmatter(installed)
    assert front["metadata.databay-cli-version"] == CLIENT_VERSION
    assert front["name"] == "databay"
    assert installed.split("---\n", 2)[2] == _skill_text().split("---\n", 2)[2]


def _version(folder: Path) -> str | None:
    return read_installed("claude", "user", folder).version


def _plant(folder: Path, version: str | None) -> None:
    folder.mkdir(parents=True)
    front = f'---\nname: databay\nmetadata:\n  databay-cli-version: "{version}"\n---\n' if version else ""
    (folder / "SKILL.md").write_text(front + "old body\n", encoding="utf-8")
    (folder / "notes.txt").write_text("kept until replaced", encoding="utf-8")


@pytest.mark.parametrize("version", ["0.0.1", None], ids=["older", "no-version"])
def test_a_copy_from_another_version_is_skill_exists_and_left_alone(cli, profile_env, version):
    folder = profile_env["home"] / ".claude/skills/databay"
    _plant(folder, version)

    run = cli("skill", "install", "--target", "claude")

    assert run.code == 2
    error = run.error()
    assert error["code"] == "skill_exists"
    assert error["details"] == {"path": str(folder), "installed_version": version, "cli_version": CLIENT_VERSION}
    assert CLIENT_VERSION in error["message"]
    assert (version or "an unknown version") in error["message"]
    assert error["next"] == "databay skill install --target claude --scope user --force"
    assert (folder / "notes.txt").is_file()
    assert _version(folder) == version


def test_force_replaces_a_copy_from_another_version(cli, profile_env):
    folder = profile_env["work"] / ".agents/skills/databay"
    _plant(folder, "0.0.1")

    run = cli("skill", "install", "--target", "codex", "--scope", "project", "--force")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["replaced_version"] == "0.0.1"
    assert _version(folder) == CLIENT_VERSION
    assert not (folder / "notes.txt").exists()
    assert sorted(p.name for p in folder.iterdir()) == ["SKILL.md", "references"]


def test_installing_the_same_version_again_needs_no_force(cli, profile_env):
    cli("skill", "install", "--target", "claude")

    run = cli("skill", "install", "--target", "claude")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["replaced_version"] == CLIENT_VERSION


def test_an_unknown_target_is_unsupported_target_and_writes_nothing(cli, profile_env):
    run = cli("skill", "install", "--target", "cursor")

    assert run.code == 2
    assert run.error()["code"] == "unsupported_target"
    assert list(profile_env["home"].iterdir()) == []
    assert list(profile_env["work"].iterdir()) == []


def test_status_says_when_no_skill_is_installed(cli, server):
    server.on("GET", "/api/auth/me", answer(200, ME))

    run = cli("status")

    assert run.code == 0, run.stderr
    assert run.json()["result"]["skill"] == {"state": "not_installed", "cli_version": CLIENT_VERSION, "installed": []}


def test_status_reports_a_current_skill(cli, server, profile_env):
    server.on("GET", "/api/auth/me", answer(200, ME))
    cli("skill", "install", "--target", "claude", "--scope", "project")

    run = cli("status")

    assert run.code == 0, run.stderr
    skill = run.json()["result"]["skill"]
    assert skill["state"] == "current"
    assert skill["installed"] == [
        {
            "target": "claude",
            "scope": "project",
            "path": str(profile_env["work"] / ".claude/skills/databay"),
            "version": CLIENT_VERSION,
            "problem": None,
            "outdated": False,
        }
    ]
    assert run.events("skill_outdated") == []


def test_status_reports_skill_outdated_for_a_copy_from_another_version(cli, server, profile_env):
    server.on("GET", "/api/auth/me", answer(200, ME))
    cli("skill", "install", "--target", "claude")
    _plant(profile_env["home"] / ".agents/skills/databay", "0.0.1")

    run = cli("status")

    assert run.code == 0, run.stderr
    skill = run.json()["result"]["skill"]
    assert skill["state"] == "skill_outdated"
    assert [(c["target"], c["version"], c["outdated"]) for c in skill["installed"]] == [
        ("claude", CLIENT_VERSION, False),
        ("codex", "0.0.1", True),
    ]
    assert run.events("skill_outdated") == [
        {
            "event": "skill_outdated",
            "path": str(profile_env["home"] / ".agents/skills/databay"),
            "installed_version": "0.0.1",
            "problem": None,
            "cli_version": CLIENT_VERSION,
            "next": "databay skill install --target codex --scope user --force",
        }
    ]


def test_status_reports_an_unreadable_skill_as_outdated_and_still_succeeds(cli, server, profile_env):
    server.on("GET", "/api/auth/me", answer(200, ME))
    folder = profile_env["home"] / ".claude/skills/databay"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(b"---\nname: \xff\n---\n")

    run = cli("status")

    assert run.code == 0, run.stderr
    skill = run.json()["result"]["skill"]
    assert skill["state"] == "skill_outdated"
    assert skill["installed"][0]["version"] is None
    assert skill["installed"][0]["problem"] == f"{folder / 'SKILL.md'} cannot be read (UnicodeDecodeError)"


def test_a_skill_with_crlf_line_endings_is_read(cli, server, profile_env):
    server.on("GET", "/api/auth/me", answer(200, ME))
    cli("skill", "install", "--target", "claude")
    skill_md = profile_env["home"] / ".claude/skills/databay/SKILL.md"
    skill_md.write_bytes(skill_md.read_bytes().replace(b"\n", b"\r\n"))

    run = cli("status")

    assert run.json()["result"]["skill"]["state"] == "current"


def test_status_and_install_both_see_a_broken_symlink(cli, server, profile_env):
    server.on("GET", "/api/auth/me", answer(200, ME))
    folder = profile_env["home"] / ".claude/skills/databay"
    folder.parent.mkdir(parents=True)
    folder.symlink_to(profile_env["home"] / "gone")

    status = cli("status")
    install = cli("skill", "install", "--target", "claude")

    assert status.json()["result"]["skill"]["installed"][0]["problem"] == f"{folder / 'SKILL.md'} does not exist"
    assert install.code == 2
    assert install.error()["code"] == "skill_exists"


def test_status_finds_a_project_copy_above_the_working_directory(cli, server, profile_env, monkeypatch):
    server.on("GET", "/api/auth/me", answer(200, ME))
    project = profile_env["work"]
    _plant(project / ".agents/skills/databay", "0.0.1")
    subdirectory = project / "src" / "app"
    subdirectory.mkdir(parents=True)
    monkeypatch.chdir(subdirectory)

    run = cli("status")

    assert run.json()["result"]["skill"]["state"] == "skill_outdated"
    [event] = run.events("skill_outdated")
    assert event["next"] == f"cd {project} && databay skill install --target codex --scope project --force"


def test_a_folder_it_cannot_write_is_skill_write_failed(cli, profile_env):
    (profile_env["home"] / ".claude").write_text("a file where the folder goes", encoding="utf-8")

    run = cli("skill", "install", "--target", "claude")

    assert run.code == 2
    error = run.error()
    assert error["code"] == "skill_write_failed"
    assert error["details"] == {"path": str(profile_env["home"] / ".claude/skills/databay")}
    assert sorted(p.name for p in profile_env["home"].iterdir()) == [".claude"]


def test_force_into_a_folder_it_cannot_write_keeps_the_old_copy(cli, profile_env):
    folder = profile_env["home"] / ".claude/skills/databay"
    _plant(folder, "0.0.1")
    folder.parent.chmod(0o500)
    try:
        run = cli("skill", "install", "--target", "claude", "--force")
    finally:
        folder.parent.chmod(0o700)

    assert run.code == 2
    assert run.error()["code"] == "skill_write_failed"
    assert _version(folder) == "0.0.1"
    assert sorted(p.name for p in folder.parent.iterdir()) == ["databay"]
