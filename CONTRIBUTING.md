# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Set up and test

```bash
cd python
uv sync --frozen
uv run --frozen ruff check
uv run --frozen ruff format --check
uv run --frozen pytest
uv run --frozen python tools/gen_client.py --check
```

Every pull request runs the same checks on Python 3.12 and 3.13 (`.github/workflows/tests.yaml`).

## Generated files

Most of the client is generated from `contract/` by `python/tools/gen_client.py`: the models,
operations, resources and CLI command modules, `api.py`, `spec.py`, `error_codes.py`, `version.py` and
the skill's reference pages. They are marked in `.gitattributes`, and each starts with "do not edit".

- Generated files come only from `contract/`. The regeneration check
  (`python tools/gen_client.py --check`) fails a pull request that edits one by hand. Change the
  generator or the hand-written code it reads, then run `python tools/gen_client.py` and commit
  what it writes.
- `contract/` changes only through the Databay server's sync, which opens a pull request holding the
  new contract and the regenerated client. Don't edit it: the release check compares every file with
  the digest `contract/SOURCE` records, and its owner reviews every change to it (`CODEOWNERS`). If
  the contract is wrong, open an issue.

## Commits

Use conventional commit messages (`fix(cli): ...`, `feat(sdk): ...`). Add a line under
`## Unreleased` in `python/CHANGELOG.md` for a change users notice.
