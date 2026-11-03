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

`databay status` prints the profile, server, product, `api_version` and user.

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

Publishing is not reversible: PyPI never lets a version be used twice. A release is a PR and then a
tag, so the tagged commit is exactly what ships.

1. The server team cuts a release of the contract and syncs it here as a release sync, so
   `contract/core.released.json` equals `contract/core.json`.
2. List the changes under `## Unreleased` in `python/CHANGELOG.md`, in a normal PR.
3. On a clean `main` equal to `origin/main`, with a git identity set, run the script with a version
   higher than the current one:

   ```bash
   python python/tools/release_client.py prepare 0.2.0
   ```

   The script commits on `release/databay-cli-v0.2.0`: it sets the version in `pyproject.toml` and
   `uv.lock`, dates the changelog section and regenerates the client. It never pushes, tags or
   publishes. It refuses a dirty tree, a commit that is not `origin/main`, a version that does not go
   up, an empty `Unreleased` section, a stale generated client, and a `contract/` that is not one
   release (below). If a later step fails, it puts the repository back where it started.
4. Push the branch, open a PR and merge it. Nothing is published yet.
5. An owner tags the merge commit. The tag is the release:

   ```bash
   git tag python/v0.2.0 <merge commit>
   git push origin python/v0.2.0
   ```

`contract/` is one release when its files match the digests in `contract/SOURCE`, the released spec
equals `core.json`, and the released meta records the same `api_version` and error codes as
`api_version.json` and `error_codes.json`.

### What the tag runs

`.github/workflows/release-databay-cli.yaml`:

1. **Refuses** a commit that is not on `main`, then runs `python/tools/release_client.py check --tag <tag>`:
   it refuses a tag that differs from the `pyproject.toml` version, a `contract/` that is not one release,
   a stale generated client, and a version with no dated changelog section.
2. **Builds** the sdist and the wheel.
3. **Tests**: ruff, the client's tests and the regeneration check, through `tests.yaml`.
4. **Smoke-tests** the wheel in a fresh venv on Python 3.12 and 3.13 (`python/tools/smoke_client_wheel.sh`):
   the skill and license files are in it, `databay --help` and `--version` work, and `databay skill
   install` writes the skill to a temporary folder.
5. **Publishes** with PyPI trusted publishing from the `pypi` environment, once the tests and the smoke
   tests have passed and its required reviewer approves. This job alone holds `id-token: write`, and no
   PyPI token is stored anywhere.

Every action in the workflows is pinned to a commit, and uv to a fixed version.
