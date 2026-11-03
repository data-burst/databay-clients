# databay-clients

Open-source SDKs and the `databay` CLI for [Databay](https://databurst.tech) servers, licensed under
Apache-2.0.

| Path | What it holds |
|---|---|
| `python/` | `databay-cli`: the Python SDK, the `databay` command and its agent skill |
| `contract/` | the Databay client contract every client is generated from, copied from the server |
| `docs/cli.md` | install, sign in and release steps |

Install the CLI and sign in:

```bash
uv tool install "git+https://github.com/data-burst/databay-clients#subdirectory=python"
databay login --server https://<host>
```

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).
