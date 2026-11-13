"""``tools/release_client.py`` on a copy of this repository, and the workflows that run it."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COPIED_FILES = (
    "python/CHANGELOG.md",
    "python/pyproject.toml",
    "python/uv.lock",
    "python/tools/contract.py",
    "python/tools/gen_client.py",
    "python/tools/release_client.py",
)
COPIED_DIRECTORIES = ("contract", "python/src")
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "release test",
    "GIT_AUTHOR_EMAIL": "release@example.com",
    "GIT_COMMITTER_NAME": "release test",
    "GIT_COMMITTER_EMAIL": "release@example.com",
}
WORKFLOWS = REPO / ".github" / "workflows"
RELEASE_WORKFLOW = WORKFLOWS / "release-databay-cli.yaml"
TESTS_WORKFLOW = WORKFLOWS / "tests.yaml"
PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, cwd=root, env={**os.environ, **GIT_IDENTITY}
    )
    return done.stdout


def _release(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "python" / "tools" / "release_client.py"), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        env={**os.environ, **GIT_IDENTITY, **(env or {})},
        timeout=120,
    )


def _generated_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "python" / "tools" / "gen_client.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        timeout=120,
    )


def _ignore_bytecode(_directory: str, names: list[str]) -> list[str]:
    return [name for name in names if name == "__pycache__"]


def _write_source(contract: Path, commit: str) -> None:
    """``SOURCE`` as the server's sync writes it."""
    names = sorted(p.name for p in contract.iterdir() if p.name != "SOURCE")
    digests = "".join(f"{hashlib.sha256((contract / n).read_bytes()).hexdigest()}  {n}\n" for n in names)
    (contract / "SOURCE").write_text(f"commit {commit}\n{digests}", encoding="utf-8")


def _release_sync(root: Path) -> None:
    """What a release sync of a cut brings: the released spec equal to core, and its meta."""
    contract = root / "contract"
    shutil.copyfile(contract / "core.json", contract / "core.released.json")
    document = json.loads((contract / "core.json").read_text(encoding="utf-8"))
    registry = json.loads((contract / "error_codes.json").read_text(encoding="utf-8"))
    version = json.loads((contract / "api_version.json").read_text(encoding="utf-8"))["api_version"]
    meta = {
        "api_version": version,
        "since": {f"{m.upper()} {path}": version for path, methods in document["paths"].items() for m in methods},
        "error_codes": sorted(registry["error_codes"]),
        "cdc_error_codes": sorted(registry["cdc_error_codes"]),
    }
    (contract / "core.released.meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", "utf-8")
    _write_source(contract, "c" * 40)


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("repo")
    for relative in COPIED_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    for relative in COPIED_DIRECTORIES:
        shutil.copytree(REPO / relative, root / relative, ignore=_ignore_bytecode)
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _release_sync(root)
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "start")
    return root


@pytest.fixture
def repo(template, tmp_path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(template, root)
    _git(tmp_path, "clone", "--quiet", "--bare", str(root), "origin.git")
    _git(root, "remote", "add", "origin", str(tmp_path / "origin.git"))
    _git(root, "fetch", "--quiet", "origin")
    return root


def _commit_and_push(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", message)
    _git(root, "push", "--quiet", "origin", "main")


def _semantic_release() -> dict:
    return tomllib.loads((REPO / "python/pyproject.toml").read_text(encoding="utf-8"))["tool"]["semantic_release"]


def _cut(root: Path, version: str) -> None:
    """What semantic-release leaves on main: the stamped version and a dated changelog section."""
    assert _release(root, "stamp", version).returncode == 0
    path = root / "python/CHANGELOG.md"
    flag = "<!-- version list -->\n"
    path.write_text(
        path.read_text(encoding="utf-8").replace(flag, f"{flag}\n## v{version} (2026-11-14)\n\n- Initial Release\n"),
        encoding="utf-8",
    )
    _commit_and_push(root, f"chore(release): databay-cli v{version} [skip ci]")


def test_stamp_writes_the_version_everywhere_and_check_accepts_the_release(repo) -> None:
    _cut(repo, "0.2.0")

    assert 'version = "0.2.0"' in (repo / "python/pyproject.toml").read_text(encoding="utf-8")
    assert 'CLIENT_VERSION = "0.2.0"' in (repo / "python/src/databay/version.py").read_text(encoding="utf-8")
    assert re.search(r'name = "databay-cli"\nversion = "0.2.0"', (repo / "python/uv.lock").read_text(encoding="utf-8"))
    assert _generated_check(repo).returncode == 0
    assert _release(repo, "check", "--tag", "python/v0.2.0").returncode == 0


def test_stamp_changes_only_the_files_semantic_release_commits(repo) -> None:
    assets = _semantic_release()["assets"]
    assert _release(repo, "stamp", "0.2.0").returncode == 0

    changed = {line[3:] for line in _git(repo, "status", "--porcelain").splitlines()}

    assert changed <= set(assets)


@pytest.mark.parametrize("version", ["0.2", "0.2.0rc1", "next"])
def test_stamp_refuses_what_is_not_a_version(repo, version) -> None:
    done = _release(repo, "stamp", version)

    assert done.returncode != 0
    assert "is not a version" in done.stderr
    assert _git(repo, "status", "--porcelain") == ""


def test_ready_accepts_a_release_sync(repo) -> None:
    assert _release(repo, "ready").returncode == 0


def test_ready_refuses_a_contract_with_no_release(repo) -> None:
    for name in ("core.released.json", "core.released.meta.json"):
        (repo / "contract" / name).unlink()
    _write_source(repo / "contract", "d" * 40)

    done = _release(repo, "ready")

    assert done.returncode != 0
    assert "contract/core.released.json does not exist" in done.stderr


@pytest.mark.parametrize(
    ("tag", "message"),
    [
        pytest.param("python/v0.3.0", "names 0.3.0, but pyproject.toml is at 0.2.0", id="another version"),
        pytest.param("v0.2.0", "does not start with python/v", id="another prefix"),
        pytest.param("python/vnext", "is not a version", id="not a version"),
    ],
)
def test_check_refuses_a_tag_that_differs_from_the_package_version(repo, tag, message) -> None:
    _cut(repo, "0.2.0")

    done = _release(repo, "check", "--tag", tag)

    assert done.returncode != 0
    assert message in done.stderr


def test_check_refuses_a_contract_file_edited_by_hand(repo) -> None:
    _cut(repo, "0.2.0")
    path = repo / "contract/error_codes.json"
    path.write_bytes(path.read_bytes() + b"\n")

    done = _release(repo, "check", "--tag", "python/v0.2.0")

    assert done.returncode != 0
    assert "error_codes.json differs from the digest SOURCE records" in done.stderr


def test_check_refuses_a_released_spec_that_differs_from_core(repo) -> None:
    _cut(repo, "0.2.0")
    path = repo / "contract/core.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["info"]["description"] = "a sync after the cut"
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _write_source(repo / "contract", "e" * 40)

    done = _release(repo, "check", "--tag", "python/v0.2.0")

    assert done.returncode != 0
    assert "core.released.json differs from core.json" in done.stderr


def test_check_refuses_an_api_version_the_release_did_not_record(repo) -> None:
    _cut(repo, "0.2.0")
    (repo / "contract/api_version.json").write_text('{\n "api_version": "1.1"\n}\n', encoding="utf-8")
    _write_source(repo / "contract", "e" * 40)

    done = _release(repo, "check", "--tag", "python/v0.2.0")

    assert done.returncode != 0
    assert "records API 1.0, but api_version.json is 1.1" in done.stderr


def test_check_refuses_a_generated_client_that_does_not_match(repo) -> None:
    _cut(repo, "0.2.0")
    path = repo / "python/src/databay/version.py"
    path.write_text(path.read_text(encoding="utf-8").replace("0.2.0", "0.1.0"), encoding="utf-8")

    done = _release(repo, "check", "--tag", "python/v0.2.0")

    assert done.returncode != 0
    assert "does not match this commit" in done.stderr
    assert "version.py" in done.stderr


def test_check_refuses_a_lock_that_records_another_version(repo) -> None:
    _cut(repo, "0.2.0")
    path = repo / "python/uv.lock"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'name = "databay-cli"\nversion = "0.2.0"', 'name = "databay-cli"\nversion = "0.1.0"'
        ),
        encoding="utf-8",
    )

    done = _release(repo, "check", "--tag", "python/v0.2.0")

    assert done.returncode != 0
    assert "uv.lock records databay-cli 0.1.0, but pyproject.toml is at 0.2.0" in done.stderr


def test_check_refuses_a_version_with_no_changelog_section(repo) -> None:
    _cut(repo, "0.2.0")
    path = repo / "python/CHANGELOG.md"
    path.write_text(re.sub(r"## v0\.2\.0 .*\n", "", path.read_text(encoding="utf-8")), encoding="utf-8")

    done = _release(repo, "check", "--tag", "python/v0.2.0")

    assert done.returncode != 0
    assert "has no section for 0.2.0" in done.stderr


def _workflow(path: Path) -> dict:
    # PyYAML reads the bare key `on` as the boolean true.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    """A workflow's triggers; `on: push` and `on: [push]` become `{"push": None}`."""
    triggers = workflow[True]
    if isinstance(triggers, str):
        return {triggers: None}
    return triggers if isinstance(triggers, dict) else dict.fromkeys(triggers)


def _all_workflows() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yaml"), *WORKFLOWS.glob("*.yml")])


def test_only_the_publish_job_can_mint_a_pypi_token() -> None:
    workflow = _workflow(RELEASE_WORKFLOW)

    holders = [name for name, job in workflow["jobs"].items() if "id-token" in job.get("permissions", {})]

    assert holders == ["publish"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["publish"]["permissions"] == {"id-token": "write"}
    assert workflow["jobs"]["publish"]["environment"]["name"] == "pypi"


def test_the_workflow_publishes_only_after_the_tests_and_the_smoke_test() -> None:
    jobs = _workflow(RELEASE_WORKFLOW)["jobs"]
    steps = "\n".join(str(step.get("run", "")) for step in jobs["build"]["steps"])

    assert sorted(jobs["publish"]["needs"]) == ["smoke", "tests"]
    assert jobs["smoke"]["needs"] == ["version", "build"]
    assert jobs["tests"]["uses"] == "./.github/workflows/tests.yaml"
    assert steps.index("python/tools/release_client.py check") < steps.index("uv build")


def test_the_tests_workflow_runs_on_pull_requests_and_can_be_called() -> None:
    workflow = _workflow(TESTS_WORKFLOW)
    steps = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["python"]["steps"])

    assert {"pull_request", "workflow_call"} <= set(_triggers(workflow))
    assert workflow["jobs"]["python"]["strategy"]["matrix"]["python"] == ["3.12", "3.13"]
    for command in ("ruff check", "ruff format --check", "pytest", "tools/gen_client.py --check"):
        assert command in steps


def test_semantic_release_tags_what_the_script_checks() -> None:
    script = (REPO / "python/tools/release_client.py").read_text(encoding="utf-8")
    prefix = re.search(r'^TAG_PREFIX = "([^"]+)"$', script, re.MULTILINE)

    assert prefix is not None
    assert _semantic_release()["tag_format"] == f"{prefix.group(1)}{{version}}"
    assert set(_triggers(_workflow(RELEASE_WORKFLOW))) == {"push", "workflow_dispatch"}
    assert _triggers(_workflow(RELEASE_WORKFLOW))["push"] == {"branches": ["main"]}


def test_semantic_release_runs_only_on_a_contract_that_is_one_release() -> None:
    steps = _workflow(RELEASE_WORKFLOW)["jobs"]["version"]["steps"]
    ready = next(i for i, step in enumerate(steps) if "release_client.py ready" in str(step.get("run", "")))
    semver = next(i for i, step in enumerate(steps) if "python-semantic-release" in str(step.get("uses", "")))

    assert ready < semver
    assert steps[semver]["if"] == "steps.ready.outputs.ready == 'true'"
    assert steps[semver]["with"]["directory"] == "python"


def test_no_other_workflow_runs_on_a_release_tag() -> None:
    running = []
    for path in _all_workflows():
        triggers = _triggers(_workflow(path))
        if path == RELEASE_WORKFLOW or "push" not in triggers:
            continue
        push = triggers["push"] or {}
        # A push with no branch or tag filter runs on every tag: GitHub does not apply `paths` to a tag.
        if "tags" in push or not {"branches", "branches-ignore"} & set(push):
            running.append(path.name)

    assert running == []


def test_every_action_is_pinned_to_a_commit_and_uv_to_a_version() -> None:
    unpinned = []
    uv_versions = set()
    for path in _all_workflows():
        for name, job in _workflow(path)["jobs"].items():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action is None:
                    continue
                if not PINNED.fullmatch(action):
                    unpinned.append(f"{path.name}:{name}: {action}")
                if action.startswith("astral-sh/setup-uv@"):
                    uv_versions.add(step.get("with", {}).get("version"))

    assert unpinned == []
    assert len(uv_versions) == 1
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(next(iter(uv_versions))))
