# Changelog

`databay-cli`: the Databay SDK and the `databay` command. New entries go under Unreleased; a release
dates them (`python/tools/release_client.py`, see `docs/cli.md`). This package's version is separate from
`api_version`, the version of the API a client speaks.

## Unreleased

### Added

- First release of the SDK and the `databay` command: sign in, projects, sources, pipes, warehouses,
  SQL queries and dashboards, generated from the Databay client contract.
- The agent skill, installed with `databay skill install`.
- `databay org list` and `databay org switch <organization>`: choose the active organization, which the
  server stores and every token names; switching refreshes the token and clears the active project.
- `databay login --org <organization>`; without it, a person in several organizations with none active
  is asked on a terminal, and gets `org_choice_required` when the CLI cannot ask.
- `databay status` reports `needs_org_choice`.
- `databay schema-change state [--pipe-uuid] [--state]`: each pipe with an unfinished schema change,
  worst first; `databay schema-change pending --status` picks the event statuses to list.
- `databay metrics list`, `databay metrics describe <metric>` and `databay metrics query <metric>` with
  `--by`, `--grain`, `--last` or `--from`/`--to`, `--where member=value` (also `!=`, `>`, `>=`, `<`, `<=`),
  `--limit`, `--warehouse`, `--model-version`, `--explain` and `--dry-run`; a query sends
  `X-Databay-Caller: cli`. `databay warehouse-access-role get-metrics|set-metrics` read and set a role's
  metric grants. The agent skill teaches list, describe, query and explain, and labels a `databay sql`
  fallback `answer_path: raw_sql`.
- On a terminal, an error answered with a 5xx prints its `trace_id`.
- Dashboards follow the server's cards model: a card belongs to the project (`dashboards-card`), is put
  on dashboards with `dashboards-placement`, and a dashboard takes `--refresh-interval-s`;
  `dashboards-home` reads or sets the project's Overview dashboard and pins cards to it.
