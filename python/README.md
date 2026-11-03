# databay-cli

The SDK and the `databay` command for a [Databay](https://databurst.tech) server. The agent skill that
ships with it lets an agent such as Claude Code drive the server through the command.

Needs Python 3.12 or newer, on Linux or macOS. The package is `databay-cli`; the `databay` project on
PyPI is unrelated.

Until the first PyPI release, install it from the repository:

```bash
uv tool install "git+https://github.com/data-burst/databay-clients#subdirectory=python"
databay login --server https://<host>
databay skill install --target claude
```

Install, sign in and releases: [docs/cli.md](https://github.com/data-burst/databay-clients/blob/main/docs/cli.md).
Changes: [CHANGELOG.md](https://github.com/data-burst/databay-clients/blob/main/python/CHANGELOG.md).

Licensed under Apache-2.0: see the `LICENSE` file in the package.
