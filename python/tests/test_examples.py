"""Every example in the client contract parses with the real CLI parser, and names a real command."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from cli_parse import parse

SPEC = Path(__file__).resolve().parents[2] / "contract" / "core.json"


def _examples(*, curated: bool = False) -> list[tuple[str, str]]:
    document = json.loads(SPEC.read_text(encoding="utf-8"))
    found = []
    for methods in document["paths"].values():
        for op in methods.values():
            declared = op["x-databay-client"]
            if declared["agent_shape"].startswith("curated:") is not curated:
                continue
            found += [(op["operationId"], example) for example in declared["examples"]]
    return found


@pytest.mark.parametrize(("operation", "example"), _examples(), ids=[e for _, e in _examples()])
def test_the_example_parses(operation, example):
    ctx = parse(example)

    assert ctx.command.callback is not None, f"{operation}: {example} stops at a group"


@pytest.mark.parametrize(("operation", "example"), _examples(curated=True), ids=[e for _, e in _examples(curated=True)])
def test_a_curated_example_parses_with_its_hand_written_command(operation, example):
    ctx = parse(example)

    assert ctx.command.callback is not None, f"{operation}: {example} stops at a group"


def test_every_direct_operation_has_an_example():
    document = json.loads(SPEC.read_text(encoding="utf-8"))
    ids = {
        op["operationId"]
        for m in document["paths"].values()
        for op in m.values()
        if op["x-databay-client"]["agent_shape"] == "direct"
    }
    assert ids == {operation for operation, _ in _examples()}


def test_a_flag_the_command_does_not_take_is_refused():
    with pytest.raises(click.UsageError):
        parse("databay pipe get <pipe_uuid> --no-such-flag 1")
