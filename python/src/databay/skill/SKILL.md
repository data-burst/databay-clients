---
name: databay
description: >-
  Drive a Databay server with the databay CLI: sign in, pick a project, add and check a
  database source, create pipes that replicate its tables into a warehouse, wait for them,
  query metrics such as net sales, define and publish metrics, query the warehouse with SQL,
  build dashboards, and connect a BI tool. Use when the user asks to move or sync data with
  Databay, create or fix a pipe, ask for or define a metric, query a Databay warehouse, build
  a dashboard, connect a BI tool, or run any `databay` command.
---

# databay

The `databay` CLI drives a Databay server: `databay <noun> <verb> [ARGS] [OPTIONS]`.
Flags, arguments and examples for every command are in `references/commands.md`; every exit
code and error code is in `references/errors.md`. Read them when a step below is not enough,
instead of guessing a flag.

## Rules

- **Always pass `--output json`.** Read the result from stdout. Progress, hints and errors
  go to stderr, one JSON object per line.
- **Never print a secret.** No password, token or API key goes on a command line, in a
  message to the user, or in a file you show. Pass a secret field with `--<field>-env NAME`
  (an environment variable the user set) or `--<field>-stdin`; for example `--password-env`.
- **Never pass `--yes` on your own.** A command that takes `--yes` deletes or overwrites
  state. Say what it will remove, wait for the user's explicit agreement, then run it with
  `--yes`.
- **Keep every wait under your shell's limit.** Pass `--timeout` on every wait, below the
  command timeout of the tool that runs it: `--timeout 540` in Claude Code, whose cap is
  600 s. Under another agent, set the tool's timeout for the call above the `--timeout` you
  pass, or pass a smaller `--timeout`. The examples below use 540.
- **Follow `next`.** A result or an error that carries `next` names the exact command to
  run next. Prefer it to building one yourself.

## Before any task

1. Check who and where you are:

   ```
   databay status --output json
   ```

   Exit 3 means there is no profile or sign-in. Ask the user to sign in once, in their own
   terminal, because it opens a browser (or prints a code with `--device`):

   ```
   databay login --server https://databay.example.com
   ```

   A machine without a browser signs in with an org API key whose secret is in a variable:
   `databay login --server https://databay.example.com --api-key-id <client_id> --api-key-env DATABAY_API_KEY`.

   `status` shows the `org` the token names. If `needs_org_choice` is true, or the user wants another
   organization, list them, ask the user which one, and switch; switching clears the active project:

   ```
   databay org list --output json
   databay org switch <organization> --output json
   ```

2. Set the project. Most commands act in the profile's active project. If `status` shows
   none, list the projects, ask the user which one, and set it by id:

   ```
   databay project list --output json
   databay project use <project_id> --output json
   ```

   `--project <project_id>` on a command that acts in a project overrides it for that command
   only. A command that names a resource by its id (`pipe get`, `pipe status`, `pipe wait`) finds
   the project from the resource and takes no `--project`.

3. If `status` reports `skill_outdated`, an installed copy of this skill is from another CLI
   version: tell the user, and offer the `next` command its `skill_outdated` event names.

## Replicate a database table into the warehouse

Run the steps in this order. Each step's check must pass before the next.

1. **Find the source.** Look for the database in `databay source list --output json`. A row has
   no host or database and the list shows only sources this project can use, so compare a likely
   row's `config` in `databay source get <source_uuid> --output json`, and ask the user when you
   cannot tell: a wrong miss makes a second source for the same database. For a match, run
   `databay source recheck <source_uuid> --output json`, then go to step 3 if it passes:
   - Exit 9 (`not_ready`) means a requirement failed: show the user each failing item and its
     fix, and stop until it is done.
   - A `source_status` such as `needs_reauth` or `deleting` holds the source whatever its checks
     say: tell the user and stop.
   - A check with status `unknown` or `not_run` could not be verified. On a type with no guided
     setup, if the user has verified it by hand, acknowledge it with
     `databay source-readiness-override set <source_uuid> --reason "<what they checked>" --output json`.
     PostgreSQL has guided setup and takes no override (`source_override_not_offered`): the
     check has to pass.
2. **Add the source.** Precheck the connection first (the password comes from a variable the
   user set). Exit 9 with a `fail` item means the same as in step 1. With only `unknown` or
   `not_run` items, ask the user whether to save it anyway. Then create it:

   ```
   databay source precheck postgres --host <host> --port 5432 --db-name <db> --username <user> --password-env PGPASSWORD --output json
   databay source create postgres --name <name> --host <host> --port 5432 --db-name <db> --username <user> --password-env PGPASSWORD --output json
   ```

   It answers with the source's `uuid`. A create is safe to repeat only with the
   `--idempotency-key` it printed. If its `status` is `setup_incomplete`, a setup check did not
   pass: run `databay source recheck <source_uuid> --output json` and handle it as in step 1. Pass `--allow-incomplete` only when the user asks to save a source Databay
   cannot reach yet. `references/commands.md` has `source create` for the other source types.

   A PostgreSQL source made here has not run its connection checks, and a new pipe on it answers
   `source_no_stored_pass` until they pass. Run them, then read the run until its `status` is no
   longer `running` (it can take over a minute):

   ```
   databay source-check-run start --source-id <source_uuid> --active-probes --output json
   databay source-check-run get <run_id> --output json
   ```

   Show the user each `fail` or `unknown` row and its `fixes`, and stop until it is done. Then run
   the same check again. A change to the source's credentials clears the result.
3. **Check the tables.** Each table says whether it can be replicated, and the fix if not:

   ```
   databay source-table check <source_uuid> public --output json
   ```

   Exit 9 again means fix first. Never create a pipe on a table that is not ready.
4. **Create the pipe.** It answers at once with the pipe's `uuid`. A create is safe to repeat
   only with the `--idempotency-key` it printed:

   ```
   databay pipe create --source-id <source_uuid> --schema-name public --table-name orders --dest-table-name orders --output json
   ```

   The `next` it prints waits only until the pipe is provisioned. Go on to step 5 instead.
5. **Wait until the table is ready, then query it.**

   ```
   databay pipe wait <pipe_uuid> --until data --timeout 540 --output json
   ```

   Exit 4 means it is not ready yet: run the `next` command the output names (the same wait; it
   creates nothing), at most 3 times at 540 s. After that, report the last state to the user.
   `healthy but no rows have landed` means the pipe works and the source has sent nothing yet.
   Exit 5 means it failed: report the reason.

   Exit 0 means the table holds rows (`wait.reason` `rows_landed`), or this warehouse has no
   way to report a row count and the pipe is healthy (`healthy_unmeasured`). Run the `next`
   command it prints, which counts the rows:

   ```
   databay sql --db <sink_db_uuid> "select count(*) from <db_name>.<dest_table_name>" --output json
   ```

   After `healthy_unmeasured`, 0 rows or a table that does not exist yet means nothing has
   landed yet. Always pass `--db`: without it the statement runs on the org's default warehouse,
   which may not be the pipe's. `sql` runs one read-only statement; it can also read
   `--file <path>`.

## Answer with a metric

For a business number (net sales, orders, sessions, ad spend), query a metric before writing
SQL: a metric is defined once, so its answer matches the dashboards. Work in this order:

1. **List** what this project can query: `databay metrics list --output json`. Each metric has
   its `dimensions`, `grains`, `filter_ops`, `warehouses` and a `state`. An empty list with
   `state` `compiling` means the model is being built: wait a minute and list again. With
   `unavailable`, `reason` is the error code that says why (look it up in `references/errors.md`).
2. **Describe** the one you will use, to see which dimensions and grains it takes:
   `databay metrics describe <metric> --output json`.
3. **Query** it, always with `--caller agent`: it tells the server an agent ran the query, so
   it is counted apart from a person's. `--by` groups by a dimension (repeat it), `--grain` by
   the time dimension, `--last 30d` or `--from`/`--to` sets the range, and `--where` filters (`channel=paid`; also
   `!=`, `>`, `>=`, `<`, `<=`; repeat it and every filter must hold), and `--order <member>:desc` with
   `--limit 10` answers a top 10. Pass `--warehouse` only
   when the metric is in several warehouses (`ambiguous_warehouse` names them).
4. **Explain** when the user asks how a number was made, or before you rely on a surprising
   one: the same query, still with `--caller agent`, plus `--explain` adds the metric's
   definition, the SQL it ran and the tables it read. `--dry-run` answers that without running the query.

`truncated: true` means only the first `row_count` rows came back: narrow the range or pass a
coarser `--grain`. `model_version_unavailable` is a retry: wait for its `Retry-After`, then run
the same command.

### Worked example

How did net sales go over the last 30 days, and how is it computed?

```
databay metrics list --output json
databay metrics describe net_sales --output json
databay metrics query net_sales --grain day --last 30d --caller agent --output json
databay metrics query net_sales --grain day --last 30d --explain --caller agent --output json
```

Report the rows, and say the answer came from the `net_sales` metric (`answer_path: metric`,
with its `model_version`).

### When there is no metric for it

`metric_not_found` means the metric is not in this project's model; its `did_you_mean` lists
close names you can query. If none of them fits, you may answer with `databay sql` against the
warehouse instead (step 5 of "Replicate a database table" shows `--db`). Label that answer `answer_path: raw_sql` when
you report it, and say it is your own query, not a defined metric, so the user knows it may
not match the dashboards. Never use SQL to get around `permission_denied` or a metric the user
cannot see: the SQL console applies the user's own grants and refuses the same columns.

## Define metrics

A project's own metrics come from its metric model, a YAML file. Metrics query the published
version; a draft changes nothing until it is published.

1. **Validate** the file: `databay metric-models validate --yaml "$(cat metrics.yml)" --output json`
   names the cubes and metrics it defines. `model_rejected` lists each field to fix.
2. **Push** it as the draft: `databay metric-models push --yaml "$(cat metrics.yml)" --output json`.
   It replaces the draft there is.
3. **Publish** the draft: `databay metric-models publish --output json`. Then `databay metrics list`
   shows them with `source` `customer`.

Defining metrics needs the `model_write` privilege and publishing needs `model_publish`;
`databay project my-access --output json` shows what this user holds. Before publishing, show the user
what it would change: `databay metric-models changes --output json` lists each metric and table the
publish adds, changes or removes.

### Edit one metric

To add, change or remove one metric without rewriting the YAML, use the form commands. Each takes the
`revision` the last answer gave; `draft_changed` means someone wrote the draft since, so read it again
and redo your change. `model_managed_in_git` means the project keeps its model in git: edit the
`semantic/` files there instead.

1. **Read** the model: `databay metric-models metric-list --output json` gives the `revision`, the
   `tables` (each with its `breakdowns`) and the `metrics`. A metric or table with `editable` false
   can only change in the YAML (`reason` says why; a write to it is `metric_needs_yaml`).
2. **Write** one metric. `aggregation` is `count` (no `column`), `count_distinct`, `sum`, `avg`, `min`
   or `max`; `conditions` filter the rows it counts (`op` is `=`, `!=`, `>`, `>=`, `<`, `<=`, `in`,
   `is_null` or `is_not_null`). A metric on a table no cube reads yet also needs `--breakdowns`, the
   columns it breaks down by (`type` `string`, `number` or `time`):

   ```
   databay metric-models metric-add --revision <revision> --metric '{"name": "order_total", "description": "Sum of order totals.", "table": "orders", "aggregation": "sum", "column": "total", "conditions": []}' --breakdowns '[{"column": "created_at", "type": "time"}, {"column": "channel", "type": "string"}]' --output json
   databay metric-models metric-update <project_id> order_total --revision <revision> --metric '{"name": "order_total", "description": "Sum of paid order totals.", "table": "orders", "aggregation": "sum", "column": "total", "conditions": [{"column": "status", "op": "=", "value": "paid"}]}' --output json
   databay metric-models set-breakdowns <project_id> orders --revision <revision> --breakdowns '[{"column": "created_at", "type": "time"}]' --output json
   ```

   `metric-update`, `metric-remove` and `set-breakdowns` take the project id as their first argument.
   Removing a metric (`metric-remove <project_id> <metric> --revision <revision>`) needs `--yes`, so ask
   first. `affected_metrics` in the answer lists the table's other metrics a breakdown change reaches:
   tell the user. `comments_dropped` true means the YAML's comments are gone.
3. **Publish** with the last answer's revision, so you publish the draft you checked:
   `databay metric-models publish --revision <revision> --output json`.

### Start from dbt

To start from the project's built dbt models instead, `databay metric-models import-dbt --dry-run --output json`
shows the YAML it would write; without `--dry-run` it stores that as the draft, and `draft_exists`
means there is one already (pass `--replace-draft` only when the user agrees to lose it). Review it with
`databay metric-models show-draft --output json`, then publish.

## Dashboards

Use the `dashboards`, `dashboards-card`, `dashboards-placement`, `dashboards-folder` and
`dashboards-dataset` nouns. A card belongs to the project and runs in a warehouse of it (for a pipe's
table, its `sink_db_uuid`); a dashboard shows the cards placed on it. Create a dashboard, create a card
placed on it, then run it:

```
databay dashboards create --name <name> --output json
databay dashboards-card create --board-id <board_id> --warehouse-id <sink_db_uuid> --title <title> --spec '{"spec_version": 1, "query": {"sql": "select count(*) as n from <dest_table_name>"}, "chart": {"type": "kpi", "y": ["n"]}}' --output json
databay dashboards run <board_id> --output json
```

A metric card shows metrics of the project's model instead of SQL, so its numbers match `metrics
query`. Its spec is `spec_version` 2, and its `warehouse-id` must be one of the metric's `warehouses`:

```
databay dashboards-card create --board-id <board_id> --warehouse-id <warehouse_id> --title "Net sales by channel" --spec '{"spec_version": 2, "query": {"metrics": ["net_sales"], "dimensions": ["channel"], "time": {"grain": "week", "default": "last_90_days"}, "order": [{"member": "net_sales", "direction": "desc"}], "limit": 10}, "chart": {"type": "bar", "x": "channel", "y": ["net_sales"]}}' --output json
```

`time.from_filter` and a filter's `from_filter` read a board filter by its key, like a SQL card's
variables. `metric_card_invalid` names the part of the spec to fix.

- **`run` exits 0 even when a card fails.** Check `error` on every entry of `cards`, and report
  any that is set instead of saying the dashboard works.
- A card's SQL names a table of that warehouse without its database, or a dataset as `ds.<name>`
  (`databay dashboards-dataset list --warehouse-id <sink_db_uuid> --output json` lists them).
- A create is safe to repeat only with the `--idempotency-key` it printed.
- Each change to a dashboard or a card takes the `--revision` its last answer gave. `revision_conflict`
  means it changed since: run `databay dashboards get <board_id> --output json` (or
  `databay dashboards-card get <card_id> --output json`), check whether your change is already
  there, and if not, send it again on the new revision.
- `feature_disabled` means native dashboards are off for this organization, and
  `warehouse_not_queryable` that this warehouse cannot run cards. Tell the user the `hint`.
- `console_not_scoped` means no card can run until support or whoever runs the server turns on
  project scoping for this organization's SQL console; an organization admin cannot, whatever
  `message` says. Creating a dashboard still works, and you only learn this when a card runs.
  Tell the user before they ask for it: the console and AI agent then act only in the active
  project, and it cannot simply be turned back off.

The built-in dashboards that come with the product are only in the web app: no command reads
them, so send the user there.

## Connect a BI tool

A BI tool (Tableau, Power BI, Metabase, DBeaver) reads the project's metrics over the PostgreSQL
protocol, signing in with an org API key that lists the project. Show the user where to connect:

```
databay api-key list --output json
databay api-key-project bi-connection <key_id> <project_id> --output json
```

The answer gives `host`, `port`, `database`, `username` and `sslmode`. The password is the key's
secret, which only the user has: never ask for it or print it. The key sees only the metrics its
warehouse access roles grant, and a query past the row cap comes back cut short without an error,
so tell the user to aggregate in the tool rather than export raw rows.

## Read a failure

A failed command exits non-zero and prints one object on stderr:

```
{"error": {"code", "message", "hint", "next", "recovery", "context", ...}}
```

Read `recovery.action` first, then the exit code for the class:

| `recovery.action` | Do |
|---|---|
| `retry` | Run the same command again, at most `recovery.max_retries` times. |
| `fix_and_retry` | Fix the input the `message` and `hint` name, then run it again. |
| `do_first` | Run `next` (sign in, set a project), then the original command. |
| `ask_human` | Stop and tell the user `message` and `hint`; do not retry. |

If `recovery.side_effects` is `partial`, `complete` or `unknown`, the server may already have
changed something: check before running a write again.

| Exit | Meaning |
|---|---|
| 0 | ok |
| 1 | the server refused; fix the input or ask the user |
| 2 | usage: a flag or argument is wrong or missing; read `hint` |
| 3 | sign-in or profile needed; ask the user to sign in (see "Before any task") |
| 4 | still pending; run `next`, the same wait |
| 5 | the wait ended in failure; report `last_error` |
| 6 | transient; retry later |
| 7 | the CLI and the server versions do not match; tell the user to upgrade |
| 8 | the CLI is outdated or hit a bug; tell the user, with `support_ref` or the crash log |
| 9 | not ready; fix what the answer names, then check again |

Look up a `code` you do not know in `references/errors.md`. Quote `context.request_id` when
the user reports a problem.

## Finding a command

Every exposed API operation has a command. Search `references/commands.md` for the noun
(`## pipe`, `## warehouse`, `## source`) or run `databay <noun> <verb> --help`. Ids are
UUIDs from a `list` or `get` result; never invent one.
