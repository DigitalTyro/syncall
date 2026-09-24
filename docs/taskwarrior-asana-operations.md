# Taskwarrior ↔ Asana operations runbook

_Last updated: 24 September 2026_

This is the practical operating guide for the DigitalTyro Taskwarrior ↔ Asana work sync.

For architecture, product requirements and roadmap, read `docs/taskwarrior-asana-project.md`.

For rich-text safety, read `docs/asana-rich-text-sync-safety.md`.

## Normal sync

From the repository root:

```sh
./scripts/sync-work
```

The wrapper:

- loads `.env`
- uses the saved `work` combination
- invokes the repository's installed `tw_asana_sync`

Do not run overlapping copies manually. A local process lock is intended to reject a second concurrent run.

## Verify/update the local development checkout

Use:

```sh
./scripts/update-verify
```

This is the expected "ready to use" verification path. It installs the current checkout into the venv, checks dependencies, runs the full test suite, Ruff lint and Ruff format checks.

The `.venv` is disposable. Persistent state is elsewhere.

## Persistent state that must be protected

Do not casually delete these while troubleshooting:

- Taskwarrior task data/configuration
- syncall preferences/configuration/correspondence state
- syncall serdes snapshots
- `asana_comments.json`
- repository `.env`

On this macOS setup, syncall preferences are normally under:

```text
~/Library/Preferences/syncall/
```

The comment cache is not authoritative, but preserving it during an incident makes diagnosis easier.

## Internal Taskwarrior UDAs

syncall injects the required internal UDA definitions at runtime. Normal sync does **not** require manually adding them to `.taskrc`.

Runtime-injected UDA definitions include:

- internal sync state: `asana_gid`, `asana_pending_comments`, `asana_comment_state`
- user-facing fields: `client`, `notes`

If the user wants to edit/report a user-facing UDA with the ordinary `task` command outside syncall's runtime overrides, persistent Taskwarrior configuration may still be useful. Do not expose internal sync-state fields as normal editing surfaces.

## Change logs

Each sync run appends to two plain-text logs. The absolute paths are printed at the start of the run. With the current `xdg` config location they are:

```text
~/.config/syncall/logs/taskwarrior-to-asana.log
~/.config/syncall/logs/asana-to-taskwarrior.log
```

`taskwarrior-to-asana.log` records every create, update, delete, and comment written to Asana.

`asana-to-taskwarrior.log` records every create, update, delete, annotation change, and local identity/comment-bookkeeping write made on Taskwarrior.

Each run adds a header with a run id and a footer with a count. A run that writes nothing says so. Records include the task name, Taskwarrior uuid, Asana gid, an Asana link when the gid is known, the operation, whether it succeeded or failed, and the before/after value of each changed field. Comment and annotation adds, amendments, and removals are listed on the task record.

These files are for review only. Sync does not read them when deciding what to write.

If a run writes more than expected, stop, keep these logs, and use `audit-asana-window` as well before changing anything.

## Read-only Asana forensic audit

The repository includes:

```sh
./scripts/audit-asana-window
```

It is read-only and uses the existing `ASANA_PERSONAL_ACCESS_TOKEN` loaded from `.env`.

Example:

```sh
./scripts/audit-asana-window \
  --workspace <WORKSPACE_GID> \
  --start 2026-09-22T08:40:03Z \
  --end 2026-09-22T08:41:08Z \
  --output-prefix ~/Desktop/syncall-audit
```

It writes:

```text
~/Desktop/syncall-audit.tasks.csv
~/Desktop/syncall-audit.stories.csv
~/Desktop/syncall-audit.json
```

### UK local-time windows

The audit parser accepts timezone-aware ISO-8601 values. It is fine to provide UK local offsets directly.

For example, on 22 September 2026 the UK is on BST (UTC+1), so these windows are equivalent:

```text
2026-09-22T09:40:03+01:00
2026-09-22T08:40:03Z
```

Do not use a naive timestamp with no timezone.

The script deliberately uses:

- current `modified_at >= window start` only to discover candidate tasks
- historical `story.created_at` to decide whether task activity occurred inside the requested window
- a current-`modified_at` fallback for writes that may not emit a visible story

This means later repairs do not erase historical activity from the audit simply because the task's current `modified_at` moved beyond the original window.

## Taskwarrior and local time

Taskwarrior resolves date/time values internally to UTC epoch values, and exported/serialized timestamps may therefore appear in UTC.

That does **not** mean the user should work in UTC.

Taskwarrior accepts local date/time input and formats displayed dates according to Taskwarrior configuration/system-local semantics. The normal UK workflow should remain local.

For syncall, the most important distinction is a date-only Asana due date:

```text
due_on = 2026-09-21
```

This is a calendar day, not a UTC instant.

During BST, local midnight for that day is:

```text
2026-09-21 00:00 Europe/London
= 2026-09-20 23:00 UTC
```

If syncall extracts the UTC calendar date it would incorrectly send `2026-09-20` back to Asana. The conversion therefore returns timezone-aware Taskwarrior values to local time before deriving `due_on`.

Regression tests cover this one-day-shift case.

Reference: <https://taskwarrior.org/docs/dates/>

## What a normal first/migration run may do

A migration/hardening run may legitimately perform local maintenance such as:

- backfilling `asana_gid`
- rebuilding or repairing `asana_comment_state`
- refreshing stale/legacy comment cache entries
- reconciling historical annotation timestamps
- recovering correspondence mappings

These maintenance operations must not cause gratuitous outbound Asana writes.

A subsequent no-change run should settle to an actual no-op aside from read/reconciliation work.

## Progress UI expectations

A long operation should never look frozen without explanation.

Expected visible stages include:

- loading Taskwarrior snapshot
- discovering Asana tasks
- loading Asana history/comments
- detecting Asana changes
- detecting Taskwarrior changes
- recovering/backfilling identity
- reconciling comment state
- applying sync changes
- saving sync state
- checking annotation history

If a stage is slow and has no visible status, treat that as a UX bug.

## Known cosmetic Taskwarrior warning

`taskw-ng` may print a traceback when parsing a Taskwarrior line such as:

```text
include default.theme
```

if it cannot resolve `default.theme` from the locations it checks.

Historically this warning has been noisy but non-fatal: sync can continue after it.

Do not confuse this parser warning with sync database corruption. It is still worth cleaning up separately so real errors are easier to see.

## Incident procedure

If a run unexpectedly writes many remote changes:

1. **Do not rerun immediately.**
2. Preserve Taskwarrior data and syncall state/caches.
3. Record the exact run start/end time.
4. Run `audit-asana-window` over that period.
5. Inspect the task/story CSVs and JSON.
6. Identify the actual field-level changes before repairing anything.
7. If rich descriptions may have been touched, fetch/preserve current Asana `html_notes`.
8. Do not delete serdes/correspondence/comment state before understanding the failure.
9. Fix the write path and add regression tests.
10. Only resume normal sync after verification passes.

The project should eventually write its own atomic persistent mutation journal so this reconstruction step becomes unnecessary.

## September 2026 incident reference

The final forensic result for the major feedback-loop incident was:

- 32 successful Asana writes
- 27 no-op/name-only writes
- 3 description rewrites that damaged rich content
- 2 one-day due-date regressions
- no comment/completion/assignment changes
- one additional attempted update failed

The user manually repaired the real changes.

The resulting safety rules are documented in `docs/taskwarrior-asana-project.md` and `docs/asana-rich-text-sync-safety.md`.

## Before changing the sync implementation

Agents/developers should read:

1. `AGENTS.md`
2. `docs/taskwarrior-asana-project.md`
3. `docs/asana-rich-text-sync-safety.md`
4. this runbook

Then inspect current code and tests rather than assuming the docs describe every implementation detail perfectly.
