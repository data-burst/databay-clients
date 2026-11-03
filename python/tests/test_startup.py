"""Start-up stays cheap: help loads one noun at most, and none of httpx, pydantic, rich or keyring."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time

import pytest

BUDGET_S = 0.3
HEAVY = ("httpx", "pydantic", "rich", "keyring")


def _env() -> dict[str, str]:
    return {**os.environ, "DATABAY_OUTPUT": "json"}


@pytest.mark.parametrize("args", [["--help"], ["pipe", "get", "--help"]], ids=["root", "pipe-get"])
def test_help_starts_in_under_300ms(args):
    times = []
    for _ in range(5):
        started = time.perf_counter()
        subprocess.run([sys.executable, "-m", "databay", *args], check=True, capture_output=True, env=_env())
        times.append(time.perf_counter() - started)

    median = statistics.median(times)
    assert median < BUDGET_S, f"databay {' '.join(args)}: median {median * 1000:.0f} ms over {times}"


@pytest.mark.parametrize("args", [["--help"], ["pipe", "get", "--help"]], ids=["root", "pipe-get"])
def test_help_imports_no_heavy_module_and_no_other_noun(args):
    script = (
        "import sys, json\n"
        "from databay.cli.main import main\n"
        f"main({args!r})\n"
        "print(json.dumps(sorted(sys.modules)), file=sys.stderr)\n"
    )
    done = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True, env=_env())
    loaded = json.loads(done.stderr.strip().splitlines()[-1])

    assert not [m for m in loaded if m.split(".")[0] in HEAVY]
    commands = [m for m in loaded if m.startswith("databay.cli.commands.")]
    models = [m for m in loaded if m.startswith("databay.models.")]
    assert commands == ([] if args == ["--help"] else ["databay.cli.commands.pipe"])
    assert models == []
