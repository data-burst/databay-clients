"""
Read the client contract in ``contract/``, which comes only from the Databay server's sync.

``contract/`` holds ``core.json`` (the exposed operations), ``error_codes.json`` (the error-code
registry), ``api_version.json``, the released spec and its meta once a release has been cut, and
``SOURCE``: the server commit it was copied from and a sha256 of each file. Generated code and the
release checks read the contract only through this module.

Standard library only. A key or line this module does not know is an error naming it, so a format
change on the server side fails at the next sync instead of being misread.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contract"
SPEC = CONTRACT / "core.json"
ERROR_CODES = CONTRACT / "error_codes.json"
API_VERSION_FILE = CONTRACT / "api_version.json"
RELEASED_SPEC = CONTRACT / "core.released.json"
SOURCE = CONTRACT / "SOURCE"

EXTENSION = "x-databay-client"
SUCCESS = ("200", "201", "202", "204")
META_KEYS = frozenset({"api_version", "since", "error_codes", "cdc_error_codes"})
SOURCE_FILES = frozenset(
    {"core.json", "error_codes.json", "api_version.json", "core.released.json", "core.released.meta.json"}
)
SOURCE_COMMIT = re.compile(r"commit ([0-9a-f]{40})")
SOURCE_DIGEST = re.compile(r"([0-9a-f]{64})  (\S+)")


class ContractError(Exception):
    """``contract/`` is malformed or incomplete; the message names the file and what is wrong."""


@dataclass(frozen=True)
class Released:
    api_version: str
    since: dict[str, str]
    error_codes: tuple[str, ...]
    cdc_error_codes: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    commit: str
    digests: dict[str, str]


def parse_api_version(value: object) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
        raise ContractError(f"an API version is 'major.minor', got {value!r}")
    major, minor = value.split(".")
    return int(major), int(minor)


def api_version_in(path: Path) -> str:
    """The ``api_version`` an ``api_version.json`` records."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"api_version"}:
        raise ContractError(f"{path.name}: expected exactly the key 'api_version'")
    parse_api_version(data["api_version"])
    return data["api_version"]


def operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every operation of a spec by ``METHOD /path``."""
    return {
        f"{method.upper()} {path}": op for path, methods in document["paths"].items() for method, op in methods.items()
    }


def meta_path(released_spec: Path) -> Path:
    return released_spec.with_name(released_spec.name.removesuffix(".json") + ".meta.json")


def load_released(released_spec: Path) -> Released | None:
    """The released contract's meta, or ``None`` before the first release; half of a release is an error."""
    meta = meta_path(released_spec)
    if not released_spec.exists() and not meta.exists():
        return None
    if not released_spec.exists() or not meta.exists():
        present, absent = (released_spec, meta) if released_spec.exists() else (meta, released_spec)
        raise ContractError(f"{present.name} exists without {absent.name}; a release writes both")
    data = json.loads(meta.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != META_KEYS:
        found = sorted(data) if isinstance(data, dict) else type(data).__name__
        raise ContractError(f"{meta.name}: expected exactly the keys {sorted(META_KEYS)}, found {found}")
    parse_api_version(data["api_version"])
    if not isinstance(data["since"], dict):
        raise ContractError(f"{meta.name}: since is not an object")
    for key in ("error_codes", "cdc_error_codes"):
        if not isinstance(data[key], list) or not all(isinstance(code, str) for code in data[key]):
            raise ContractError(f"{meta.name}: {key} is not a list of strings")
    for key, value in data["since"].items():
        try:
            parse_api_version(value)
        except ContractError as exc:
            raise ContractError(f"{meta.name}: since of {key}: {exc}") from exc
    spec = json.loads(released_spec.read_text(encoding="utf-8"))
    differ = sorted(set(data["since"]) ^ set(operations(spec)))
    if differ:
        raise ContractError(f"{meta.name} and {released_spec.name} disagree on these operations: {differ}")
    return Released(
        api_version=data["api_version"],
        since=data["since"],
        error_codes=tuple(data["error_codes"]),
        cdc_error_codes=tuple(data["cdc_error_codes"]),
    )


def since_by_operation(document: dict[str, Any], released: Released | None, version: str) -> dict[str, str]:
    """The ``since`` of each operation: what the release recorded, or ``version`` for one it does not hold."""
    recorded = released.since if released is not None else {}
    return {key: recorded.get(key, version) for key in operations(document)}


def registry_codes(registry: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tuple(sorted(registry["error_codes"])), tuple(sorted(registry["cdc_error_codes"]))


def load_source(path: Path) -> Source:
    """``SOURCE``: a ``commit <sha>`` line, then ``<sha256>  <file>`` for each file the sync wrote."""
    if not path.exists():
        raise ContractError(f"{path.name} does not exist; contract/ comes only from the server's sync")
    first, *rest = path.read_text(encoding="utf-8").splitlines() or [""]
    commit = SOURCE_COMMIT.fullmatch(first)
    if commit is None:
        raise ContractError(f"{path.name}: the first line is not 'commit <sha>': {first!r}")
    digests: dict[str, str] = {}
    for number, line in enumerate(rest, start=2):
        matched = SOURCE_DIGEST.fullmatch(line)
        if matched is None or matched.group(2) not in SOURCE_FILES or matched.group(2) in digests:
            raise ContractError(f"{path.name}:{number}: not a '<sha256>  <contract file>' line: {line!r}")
        digests[matched.group(2)] = matched.group(1)
    return Source(commit=commit.group(1), digests=digests)


def source_mismatches(contract: Path, source: Source) -> list[str]:
    """Each file of ``contract`` that differs from ``SOURCE``: a changed digest, a missing or an unlisted file."""
    present = {p.name for p in contract.iterdir() if p.name != SOURCE.name}
    problems = [f"{name} is not listed in SOURCE" for name in sorted(present - set(source.digests))]
    for name, digest in sorted(source.digests.items()):
        path = contract / name
        if not path.exists():
            problems.append(f"{name} is listed in SOURCE but missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name} differs from the digest SOURCE records")
    return problems
