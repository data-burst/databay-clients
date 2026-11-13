# The `databay` CLI and SDK

`python/` holds one package with the Python SDK and the `databay` command, generated from the Databay
client contract in `contract/`.

## Install

The package is **`databay-cli`**; the command is `databay`. `databay` on PyPI is an unrelated project.
Never install it.

Nothing has been published to PyPI yet, so install from this repository. uv fetches a suitable Python:

```bash
uv tool install "git+https://github.com/data-burst/databay-clients#subdirectory=python"
uv tool install --force "git+https://github.com/data-burst/databay-clients#subdirectory=python"   # later
```

Don't run `uv tool install databay-cli` yet: until the first publish, that name on PyPI is not ours.
The first publish replaces this section with the PyPI commands.

## Sign in

```bash
databay login --server https://<host>            # a browser opens
databay login --server https://<host> --device   # over SSH or in a container: a link and a code
databay skill install --target claude            # or codex: lets an agent drive the CLI
databay status
```

`databay status` prints the profile, server, product, `api_version`, user and the organization the
token names.

## Organizations

The server stores one active organization per person, and every token names it. A person in several
organizations picks one at sign-in with `--org` (a key or a display name); without it, `databay login`
asks on a terminal when none is active yet, and stops with `org_choice_required` (still signed in)
when it cannot ask. Later:

```bash
databay org list                     # every organization you belong to; is_active marks the active one
databay org switch <organization>    # store it as active, refresh the token, clear the active project
```

A profile whose token comes from `DATABAY_TOKEN` cannot be refreshed by the CLI: the switch is stored,
and the next token you mint names the new organization. An API key belongs to one organization and
cannot switch.

A person's sign-in asks for `offline_access`, so the CLI can renew its session without a new login.
If the server's identity provider does not grant it, `databay login` stops with
`offline_access_not_granted`: whoever runs the server gives your user that role.

## Sign out

```bash
databay logout
```

`databay logout` revokes the profile's session at the identity provider and deletes the stored
credential. When the revocation fails (`revoke_failed`), the local credential is still deleted, and
whoever runs the server can end the session from the identity provider's list of the user's sessions.

## Versions

Two versions, which move separately:

- **The package version**, in `python/pyproject.toml`. `src/databay/version.py` is generated
  from it. Tags and the changelog use this one.
- **`api_version`** (`1.0`), in `contract/api_version.json`. The server reports it from
  `GET /api/auth/client-config`. A client refuses a server on another major (`server_version_mismatch`)
  and a command whose `since` is newer than the server (`server_too_old`).

Each operation records the API version it first appeared in, its `since`. The generator takes it from
the released contract's `contract/core.released.meta.json`, and an operation that release did not hold
gets the current `api_version`.

## The contract

`contract/` is a copy of the server's client contract. It changes only through the server team's sync,
which opens one PR holding the new contract and the client regenerated from it. `contract/SOURCE`
records the server commit it was copied from and a sha256 of each file. See `CONTRIBUTING.md`.

## Releasing

Publishing is not reversible: PyPI never lets a version be used twice. Releases are automatic: each push
to `main` runs `.github/workflows/release-databay-cli.yaml`, and
[python-semantic-release](https://python-semantic-release.readthedocs.io/) (configured under
`[tool.semantic_release]` in `python/pyproject.toml`) picks the next version
from the conventional commits since the last `python/v*` tag: `feat` is a minor release, `fix` and `perf`
a patch, a `!` after the type a major one, and anything else no release.

### What a push to main runs

1. **Versions**, when there is a `feat`, `fix` or `perf` commit since the last tag: semantic-release runs
   `release_client.py stamp <version>` (the version in `pyproject.toml` and `uv.lock`, then the
   regenerated client), adds the version's section to `python/CHANGELOG.md`, commits that to `main` as
   `chore(release): databay-cli v<version> [skip ci]`, tags it `python/v<version>` and opens a GitHub
   release. The bot that pushes needs leave to push to `main`.
2. **Refuses** a tagged commit that is not on `main`, then runs `release_client.py check --tag <tag>`:
   it refuses a tag that differs from the `pyproject.toml` version, a `contract/` whose files differ from
   the digests in `contract/SOURCE`, a stale generated client, and a version with no dated changelog section.
3. **Builds** the sdist and the wheel.
4. **Tests** the tagged commit: ruff, the client's tests and the regeneration check, through `tests.yaml`.
5. **Smoke-tests** the wheel in a fresh venv on Python 3.12 and 3.13 (`python/tools/smoke_client_wheel.sh`):
   the skill and license files are in it, `databay --help` and `--version` work, and `databay skill
   install` writes the skill to a temporary folder.
6. **Publishes** with PyPI trusted publishing from the `pypi` environment, once the tests and the smoke
   tests have passed and its required reviewer approves. This job alone holds `id-token: write`, and no
   PyPI token is stored anywhere.

If publishing fails after the tag exists, run the workflow by hand (**Run workflow**) with that tag,
like `python/v0.2.0`: it skips versioning and builds, tests and publishes the tag again.

Every action in the workflows is pinned to a commit, and uv to a fixed version.
