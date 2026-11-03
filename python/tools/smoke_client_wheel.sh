#!/usr/bin/env bash
# Install a built databay-cli wheel into a fresh venv, away from the checkout, and run it.
# Usage: python/tools/smoke_client_wheel.sh <wheel> <python version> <expected package version>
set -euo pipefail

wheel=$(realpath "$1")
python_version=$2
expected=$3
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

python3 -m zipfile -l "$wheel" >"$work/listing"
for file in databay/skill/SKILL.md databay/skill/references/commands.md databay/skill/references/errors.md licenses/LICENSE; do
  grep -q "$file" "$work/listing" || {
    echo "the wheel does not hold $file" >&2
    exit 1
  }
done

cd "$work"
uv venv --quiet --python "$python_version" venv
uv pip install --quiet --python venv/bin/python "$wheel"
databay=$work/venv/bin/databay

"$databay" --help >/dev/null
"$databay" --version | grep -q "databay $expected " || {
  echo "the wheel reports $("$databay" --version), expected $expected" >&2
  exit 1
}

mkdir target
(cd target && "$databay" skill install --target claude --scope project --output json)
for file in SKILL.md references/commands.md references/errors.md; do
  test -s "target/.claude/skills/databay/$file" || {
    echo "skill install did not write $file" >&2
    exit 1
  }
done
grep -q "$expected" target/.claude/skills/databay/SKILL.md || {
  echo "the installed SKILL.md does not record version $expected" >&2
  exit 1
}
echo "smoke test passed: databay-cli $expected on Python $python_version"
