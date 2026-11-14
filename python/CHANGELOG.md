# Changelog

`databay-cli`: the Databay SDK and the `databay` command. semantic-release writes each version's section from
the conventional commits since the one before (see `docs/cli.md`). This package's version is separate from
`api_version`, the version of the API a client speaks.

<!-- version list -->

## v0.1.1 (2026-11-14)

### Bug Fixes

- **readme**: Install databay-cli from PyPI
  ([`2f1bdd9`](https://github.com/data-burst/databay-clients/commit/2f1bdd972036d2930a434dba1cfccf96f5d69f0f))

### Continuous Integration

- **release**: Attach no files to the GitHub release
  ([`6b0d8b4`](https://github.com/data-burst/databay-clients/commit/6b0d8b4d0596642a7c767ef1b02a61c6ab667d07))


## v0.1.0 (2026-11-13)

- Initial Release

## What the first release holds

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
  `--order member[:desc]`, `--limit`, `--warehouse`, `--model-version`, `--explain` and `--dry-run`; a query sends
  `X-Databay-Caller: cli`, or `agent` with `--caller agent`, which the agent skill passes on every query.
  `databay warehouse-access-role get-metrics|set-metrics` read and set a role's
  metric grants. The agent skill teaches list, describe, query and explain, and labels a `databay sql`
  fallback `answer_path: raw_sql`.
- `databay metric-models push|validate|publish|show-draft|show-published|list|import-dbt`: a project's own
  metric model, which the agent skill teaches (validate, push, publish, import from dbt).
  `databay project-privilege list|grant|revoke`: a member's privileges in a project.
- `databay metric-models metric-list|metric-add|metric-update|metric-remove|set-breakdowns|changes`: edit one
  metric or one table's breakdowns on the draft's `--revision` (`draft_changed` when it moved), and see what a
  publish changes; `publish --revision` publishes the draft that was checked. `databay project my-access` shows
  the caller's role and privileges. Dashboard cards take metric specs (`spec_version` 2).
- `databay api-key-project bi-connection <key_id> <project_id>`: where a BI tool signs in with an API key.
- The agent skill teaches the metric form commands, metric cards and connecting a BI tool.
- On a terminal, an error answered with a 5xx prints its `trace_id`.
- Dashboards follow the server's cards model: a card belongs to the project (`dashboards-card`), is put
  on dashboards with `dashboards-placement`, and a dashboard takes `--refresh-interval-s`;
  `dashboards-home` reads or sets the project's Overview dashboard and pins cards to it.
