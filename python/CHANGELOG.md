# Changelog

`databay-cli`: the Databay SDK and the `databay` command. New entries go under Unreleased; a release
dates them (`python/tools/release_client.py`, see `docs/cli.md`). This package's version is separate from
`api_version`, the version of the API a client speaks.

## Unreleased

### Added

- First release of the SDK and the `databay` command: sign in, projects, sources, pipes, warehouses,
  SQL queries, dashboards and Metabase, generated from the Databay client contract.
- The agent skill, installed with `databay skill install`.
- `databay org list` and `databay org switch <organization>`: choose the active organization, which the
  server stores and every token names; switching refreshes the token and clears the active project.
- `databay login --org <organization>`; without it, a person in several organizations with none active
  is asked on a terminal, and gets `org_choice_required` when the CLI cannot ask.
- `databay status` reports `needs_org_choice`.
