"""The committed generated code is exactly what the generator writes, and the generator refuses what it cannot read."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from databay.models.pipe import PipeV2StatusResponse
from databay.models.shared import BoardOut, CardSpec, PipeV2Status
from databay.spec import API_VERSION
from databay.version import CLIENT_VERSION

REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "python" / "tools" / "gen_client.py"
REGISTRY = REPO / "contract" / "error_codes.json"
SPEC = REPO / "contract" / "core.json"


def _generate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args], capture_output=True, text=True, check=False, cwd=REPO
    )


def test_regenerating_changes_nothing():
    done = _generate("--check")

    assert done.returncode == 0, done.stdout + done.stderr


def _registry(tmp_path: Path, data: object) -> str:
    path = tmp_path / "error_codes.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_a_registry_of_bare_code_lists_is_refused(tmp_path):
    path = _registry(tmp_path, {"error_codes": ["pipe_not_found"], "cdc_error_codes": ["resync_required"]})

    done = _generate("--check", "--error-codes", path)

    assert done.returncode != 0
    assert "has no hint, action or side effects" in done.stderr


def test_a_registry_entry_with_an_unknown_action_is_refused(tmp_path):
    real = json.loads(REGISTRY.read_text(encoding="utf-8"))
    real["error_codes"]["pipe_not_found"]["action"] = "shrug"

    done = _generate("--check", "--error-codes", _registry(tmp_path, real))

    assert done.returncode != 0
    assert "error_codes.pipe_not_found: action 'shrug'" in done.stderr


def test_a_client_code_that_shadows_a_server_code_is_refused(tmp_path):
    real = json.loads(REGISTRY.read_text(encoding="utf-8"))
    real["error_codes"]["wait_pending"] = {"hint": "x", "action": "retry", "side_effects": "none", "max_retries": None}

    done = _generate("--check", "--error-codes", _registry(tmp_path, real))

    assert done.returncode != 0
    assert "['wait_pending'] are also server codes" in done.stderr


def _spec_with_source_create(tmp_path: Path, change: Callable[[dict[str, Any]], None]) -> str:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    change(spec["components"]["schemas"]["ConnectedSourceCreate"])
    path = tmp_path / "core.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("field", "schema", "collides"),
    [
        pytest.param("host", {"type": "string"}, "host", id="the same name as a variant's field"),
        pytest.param("password_env", {"type": "string"}, "password-env", id="the flag a variant's secret is read with"),
        pytest.param("project", {"type": "string"}, "project", id="a flag the CLI keeps for itself"),
    ],
)
def test_a_flag_shared_by_a_body_and_its_variant_is_a_generation_error(tmp_path, field, schema, collides):
    def add(source_create: dict[str, Any]) -> None:
        source_create["properties"][field] = schema

    done = _generate("--check", "--spec", _spec_with_source_create(tmp_path, add))

    assert done.returncode != 0
    assert "source_create" in done.stderr
    assert f"'{collides}'" in done.stderr
    assert "collide" in done.stderr


def test_a_field_colliding_with_the_off_flag_of_a_boolean_is_a_generation_error(tmp_path):
    def add(source_create: dict[str, Any]) -> None:
        source_create["properties"]["no_allow_incomplete"] = {"type": "string"}

    done = _generate("--check", "--spec", _spec_with_source_create(tmp_path, add))

    assert done.returncode != 0
    assert "'no-allow-incomplete'" in done.stderr


def test_a_body_with_two_discriminated_properties_is_refused(tmp_path):
    def add(source_create: dict[str, Any]) -> None:
        source_create["properties"]["other_config"] = source_create["properties"]["config"]

    done = _generate("--check", "--spec", _spec_with_source_create(tmp_path, add))

    assert done.returncode != 0
    assert "two discriminated properties" in done.stderr


def test_an_optional_discriminated_property_is_refused(tmp_path):
    def drop(source_create: dict[str, Any]) -> None:
        source_create["required"].remove("config")

    done = _generate("--check", "--spec", _spec_with_source_create(tmp_path, drop))

    assert done.returncode != 0
    assert "optional discriminated property config" in done.stderr


def test_an_enum_value_this_client_does_not_know_is_kept_as_unknown():
    status = PipeV2StatusResponse.model_validate(
        {"pipe_uuid": "p", "pipe_status": "migrating", "health": "healthy", "added_later": 1}
    )

    assert status.pipe_status == "migrating"
    assert status.pipe_status.is_unknown
    assert status.pipe_status not in list(PipeV2Status)
    assert not PipeV2Status("active").is_unknown


BOARD = {
    "id": "b",
    "project_id": "p",
    "folder_id": None,
    "warehouse_id": "w",
    "name": "n",
    "description": None,
    "revision": 1,
    "source_builtin": None,
    "updated_by": "u",
    "updated_at": "2026-10-31T00:00:00Z",
    "filters": [],
    "layout": [],
    "cards": [],
    "created_by": "u",
    "created_at": "2026-10-31T00:00:00Z",
}


def test_a_filter_type_this_client_does_not_know_refuses_the_whole_board():
    BoardOut.model_validate(BOARD)

    with pytest.raises(ValidationError) as caught:
        BoardOut.model_validate({**BOARD, "filters": [{"type": "text", "key": "k"}]})

    assert caught.value.errors()[0]["loc"][0] == "filters"


def test_a_spec_version_this_client_does_not_know_is_refused():
    with pytest.raises(ValidationError) as caught:
        CardSpec.model_validate({"spec_version": 2, "query": {"sql": "select 1"}, "chart": {"type": "table"}})

    assert "spec_version" in {error["loc"][0] for error in caught.value.errors()}


def _load_generator(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("gen_client_paths", GENERATOR)
    assert spec is not None
    assert spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up in sys.modules while the module runs.
    monkeypatch.setitem(sys.modules, spec.name, generator)
    spec.loader.exec_module(generator)
    return generator


def _operation_since(files: dict[Path, str], module: str, constant: str) -> str:
    text = next(text for path, text in files.items() if path.parent.name == "operations" and path.stem == module)
    block = text.partition(f"{constant} = Operation(")[2]
    found = re.search(r"since='([^']+)'", block)
    assert found is not None
    return found.group(1)


def _write_released(tmp_path: Path, spec: dict[str, Any], since: dict[str, str]) -> Path:
    path = tmp_path / "core.released.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    meta = {"api_version": "1.0", "since": since, "error_codes": [], "cdc_error_codes": []}
    (tmp_path / "core.released.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def test_without_a_released_spec_every_operation_is_new_in_the_current_api_version(monkeypatch, tmp_path):
    generator = _load_generator(monkeypatch)

    files = generator.generate(SPEC, REGISTRY, tmp_path / "core.released.json")

    assert _operation_since(files, "pipe", "PIPE_STATUS") == API_VERSION


def test_a_released_operation_keeps_its_since_and_one_absent_from_the_release_gets_the_current_version(
    monkeypatch, tmp_path
):
    generator = _load_generator(monkeypatch)
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    released = json.loads(json.dumps(spec))
    del released["paths"]["/api/auth/api-keys"]["get"]
    since = {f"{method.upper()} {path}": "0.9" for path, methods in released["paths"].items() for method in methods}

    files = generator.generate(SPEC, REGISTRY, _write_released(tmp_path, released, since))

    assert _operation_since(files, "pipe", "PIPE_STATUS") == "0.9"
    assert _operation_since(files, "api_key", "API_KEY_LIST") == API_VERSION


def test_a_released_spec_without_its_meta_is_a_generation_error(tmp_path):
    path = tmp_path / "core.released.json"
    path.write_text(SPEC.read_text(encoding="utf-8"), encoding="utf-8")

    done = _generate("--check", "--released-spec", str(path))

    assert done.returncode != 0
    assert "core.released.json exists without core.released.meta.json" in done.stderr


def test_the_package_version_matches_pyproject():
    project = tomllib.loads((REPO / "python" / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == CLIENT_VERSION


def test_every_generated_file_is_marked_linguist_generated(monkeypatch):
    generator = _load_generator(monkeypatch)
    paths = sorted(str(p.relative_to(REPO)) for p in generator.generated_paths())
    paths.append("python/uv.lock")

    done = subprocess.run(
        ["git", "-C", str(REPO), "check-attr", "linguist-generated", "--stdin"],
        input="\n".join(paths) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )

    unmarked = [line for line in done.stdout.splitlines() if not line.endswith(": true")]
    assert len(paths) > 200
    assert unmarked == []
