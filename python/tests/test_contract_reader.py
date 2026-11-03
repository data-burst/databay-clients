"""``tools/contract.py`` reads ``contract/`` strictly, and the committed contract is what its sync wrote."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
READER = REPO / "python" / "tools" / "contract.py"
CONTRACT = REPO / "contract"


@pytest.fixture(scope="module")
def reader() -> Any:
    spec = importlib.util.spec_from_file_location("contract_reader", READER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _released(tmp_path: Path, meta: dict[str, Any]) -> Path:
    spec = {"paths": {"/a": {"get": {}}}}
    path = tmp_path / "core.released.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    (tmp_path / "core.released.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def _meta(**extra: Any) -> dict[str, Any]:
    return {"api_version": "1.0", "since": {"GET /a": "1.0"}, "error_codes": [], "cdc_error_codes": [], **extra}


def test_the_committed_contract_matches_the_digests_its_sync_recorded(reader) -> None:
    source = reader.load_source(CONTRACT / "SOURCE")

    assert reader.source_mismatches(CONTRACT, source) == []
    assert {"core.json", "error_codes.json", "api_version.json"} <= set(source.digests)


def test_a_meta_key_the_reader_does_not_know_is_an_error(reader, tmp_path) -> None:
    path = _released(tmp_path, _meta(deprecated={}))

    with pytest.raises(reader.ContractError, match="expected exactly the keys"):
        reader.load_released(path)


def test_a_meta_missing_a_key_is_an_error(reader, tmp_path) -> None:
    meta = _meta()
    del meta["since"]

    with pytest.raises(reader.ContractError, match="expected exactly the keys"):
        reader.load_released(_released(tmp_path, meta))


def test_a_meta_that_disagrees_with_its_spec_is_an_error(reader, tmp_path) -> None:
    path = _released(tmp_path, _meta(since={"GET /b": "1.0"}))

    with pytest.raises(reader.ContractError, match="disagree on these operations"):
        reader.load_released(path)


def test_an_api_version_file_with_another_key_is_an_error(reader, tmp_path) -> None:
    path = tmp_path / "api_version.json"
    path.write_text(json.dumps({"api_version": "1.0", "minimum": "1.0"}), encoding="utf-8")

    with pytest.raises(reader.ContractError, match="expected exactly the key 'api_version'"):
        reader.api_version_in(path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("main\n", "the first line is not 'commit <sha>'", id="no commit line"),
        pytest.param(f"commit {'a' * 40}\n{'b' * 64}  notes.txt\n", "SOURCE:2", id="a file sync never writes"),
        pytest.param(f"commit {'a' * 40}\n{'b' * 63}  core.json\n", "SOURCE:2", id="a short digest"),
        pytest.param(
            f"commit {'a' * 40}\n{'b' * 64}  core.json\n{'c' * 64}  core.json\n", "SOURCE:3", id="a file twice"
        ),
    ],
)
def test_a_source_line_the_reader_does_not_know_is_an_error(reader, tmp_path, text, message) -> None:
    path = tmp_path / "SOURCE"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(reader.ContractError, match=message):
        reader.load_source(path)


def test_a_contract_file_edited_by_hand_differs_from_source(reader, tmp_path) -> None:
    contract = tmp_path / "contract"
    shutil.copytree(CONTRACT, contract)
    with (contract / "error_codes.json").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    (contract / "notes.txt").write_text("x", encoding="utf-8")

    found = reader.source_mismatches(contract, reader.load_source(contract / "SOURCE"))

    assert found == ["notes.txt is not listed in SOURCE", "error_codes.json differs from the digest SOURCE records"]


def test_a_file_source_lists_but_contract_lacks_is_reported(reader, tmp_path) -> None:
    contract = tmp_path / "contract"
    shutil.copytree(CONTRACT, contract)
    digest = hashlib.sha256(b"{}").hexdigest()
    with (contract / "SOURCE").open("a", encoding="utf-8") as handle:
        handle.write(f"{digest}  core.released.json\n")

    found = reader.source_mismatches(contract, reader.load_source(contract / "SOURCE"))

    assert found == ["core.released.json is listed in SOURCE but missing"]
