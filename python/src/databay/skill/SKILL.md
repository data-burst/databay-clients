---
name: databay
description: >-
  Drive a Databay server with the databay CLI: sign in, pick a project, add and check a
  database source, create pipes that replicate its tables into a warehouse, wait for them,
  query the warehouse with SQL, and build dashboards. Use when the
  user asks to move or sync data with Databay, create or fix a pipe, query a Databay
  warehouse, build a dashboard, or run any `databay` command.
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

   `--project <project_id>` on one command overrides it for that command only.

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

## Dashboards

Use the `dashboards`, `dashboards-card`, `dashboards-folder` and `dashboards-dataset` nouns. Create a
dashboard on the warehouse that holds the table (for a pipe's table, its `sink_db_uuid`), add a card to
it, then run it:

```
databay dashboards create --name <name> --warehouse-id <sink_db_uuid> --output json
databay dashboards-card create <board_id> --title <title> --revision <revision> --spec '{"spec_version": 1, "query": {"sql": "select count(*) as n from <dest_table_name>"}, "chart": {"type": "kpi", "y": ["n"]}}' --output json
databay dashboards run <board_id> --output json
```

- **`run` exits 0 even when a card fails.** Check `error` on every entry of `cards`, and report
  any that is set instead of saying the dashboard works.
- A card's SQL names a table of that warehouse without its database, or a dataset as `ds.<name>`
  (`databay dashboards-dataset list --warehouse-id <sink_db_uuid> --output json` lists them).
- A create is safe to repeat only with the `--idempotency-key` it printed.
- Each change to a dashboard takes the `--revision` its last answer gave. `revision_conflict`
  means it changed since: run `databay dashboards get <board_id> --output json`, check whether
  your change is already there, and if not, send it again on the new revision.
- `feature_disabled` means native dashboards are off for this organization, and
  `warehouse_not_queryable` that this warehouse cannot run cards. Tell the user the `hint`.
- `console_not_scoped` means no card can run until support or whoever runs the server turns on
  project scoping for this organization's SQL console; an organization admin cannot, whatever
  `message` says. Creating a dashboard still works, and you only learn this when a card runs.
  Tell the user before they ask for it: the console and AI agent then act only in the active
  project, and it cannot simply be turned back off.

The built-in dashboards that come with the product are only in the web app: no command reads
them, so send the user there.

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
