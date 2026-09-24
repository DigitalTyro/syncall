# [Taskwarrior](https://taskwarrior.org/) ⬄ [Asana](https://www.asana.com)

## Description

Synchronize Asana tasks with Taskwarrior while preserving Asana-only data that Taskwarrior cannot safely represent.

This fork uses Asana as the shared operational system and Taskwarrior as the local planning/reporting mirror.

For the full project design and roadmap, see:

- `docs/taskwarrior-asana-project.md`
- `docs/taskwarrior-asana-operations.md`
- `docs/asana-rich-text-sync-safety.md`

## Current field behaviour

| Asana | Taskwarrior | Current behaviour |
| --- | --- | --- |
| task GID | `asana_gid` | durable identity |
| `[Client] Task name` | `client:Client` + clean `description` | two-way |
| task `name` | `description` | two-way |
| `completed` | `status` | two-way |
| `created_at` | `entry` | historical source date preserved |
| `completed_at` | `end` | historical source date preserved |
| `modified_at` | `modified` | generic change/conflict metadata |
| `due_at` / `due_on` | `due` | two-way with local-calendar protection |
| `html_notes` | `notes` | Asana → TW projection for existing tasks |
| comments | annotations | Asana → TW + append-only genuinely new TW annotations → Asana |

The workspace sync includes tasks assigned to the authenticated user as well as follower-only tasks. Follower discovery uses Asana workspace search and manual pagination so result sets larger than 100 tasks are not silently truncated.

## Rich descriptions: important safety rule

Taskwarrior does **not** contain a lossless serialization of Asana rich text.

An Asana description can contain embedded images, mentions, rich links, formatting and Asana-specific metadata that a Taskwarrior Markdown/string field cannot faithfully preserve.

Therefore, for an **existing Asana task**:

- Asana `html_notes` is canonical.
- syncall projects it into readable Taskwarrior `notes`.
- normal Taskwarrior → Asana updates deliberately do **not** replace the existing Asana `html_notes`.

This is intentional protection against silent data loss.

New Taskwarrior → Asana task creation may still use Taskwarrior notes as the initial Asana description because no pre-existing Asana rich document exists.

Future two-way notes support must use field-specific baseline/merge logic rather than replacing the whole rich document. See `docs/asana-rich-text-sync-safety.md`.

## Comments and annotations

Existing Asana comments are projected into Taskwarrior annotations.

For existing mapped tasks:

- a genuinely new Taskwarrior annotation may append one new Asana comment
- editing an imported Taskwarrior annotation does not edit/recreate the Asana comment
- deleting an imported annotation does not delete the Asana comment
- duplicate prevention uses durable comment identity plus live Asana checks

Taskwarrior stores durable per-task comment reconciliation state in the internal `asana_comment_state` UDA.

This state is designed to self-heal from live Asana history if local caches/preferences are missing or stale.

## Identity and deletion safety

Mapped Taskwarrior tasks carry the Asana task GID in the internal `asana_gid` UDA.

A task disappearing from filtered discovery is not automatically treated as deleted. syncall verifies mapped missing tasks directly before propagating deletion.

This prevents assignment/follower/scope changes from becoming accidental remote deletions.

## Date/time behaviour

Taskwarrior may serialize timestamps in UTC, but user-facing work remains local-time based.

A date-only Asana `due_on` is a calendar day, not a UTC instant. syncall converts timezone-aware Taskwarrior values back to local time before deriving an Asana date so UK/BST dates do not shift backwards by one day.

## Current limitations

- Existing Asana rich descriptions cannot yet be safely edited from Taskwarrior.
- Existing Asana comments cannot be edited/deleted from Taskwarrior.
- Does not sync Asana tags, projects, likes, subtasks or arbitrary custom fields.
- Does not sync Taskwarrior tags/projects as remote fields.
- Follower-only discovery requires access to Asana workspace task search.
- The generic whole-task conflict strategy remains separate from future field-specific rich-text conflict handling.

## Setup and usage

You can synchronize Taskwarrior tasks selected by tags/project/filtering or use `--sync-all-tw-tasks`.

The current personal work profile uses the `+asana` Taskwarrior population and a saved combination named `work`.

### Normal work sync in this fork

```sh
./scripts/sync-work
```

The wrapper loads `.env` and invokes the saved `work` combination.

### Authentication

Generate an Asana [Personal Access Token](https://developers.asana.com/docs/personal-access-token).

Make it available either through:

- `ASANA_PERSONAL_ACCESS_TOKEN`
- password-store via `--token-pass-path`

The local wrapper expects the PAT to be available through `.env`.

### Find available workspaces

```sh
tw_asana_sync --list-asana-workspaces
```

### Generic workspace sync

```sh
tw_asana_sync \
  --taskwarrior-tags asana \
  --asana-workspace-gid 123456789012345
```

Or by workspace name:

```sh
tw_asana_sync \
  --taskwarrior-tags asana \
  --asana-workspace-name my-workspace
```

## Taskwarrior UDAs

syncall injects the UDA definitions it needs while it is running, including internal identity/recovery fields.

You do **not** need to manually configure internal fields such as:

- `asana_gid`
- `asana_pending_comments`
- `asana_comment_state`

If you want to view/edit user-facing fields such as `client` or `notes` with normal Taskwarrior commands outside syncall's runtime overrides, persistent Taskwarrior UDA configuration may still be convenient.

## Read-only incident audit

This fork includes:

```sh
./scripts/audit-asana-window --help
```

It reconstructs task/story activity for a timezone-aware ISO-8601 window without modifying Asana.

See `docs/taskwarrior-asana-operations.md` for examples, including UK local-time windows.

## Verification

Before using behavioural changes locally:

```sh
./scripts/update-verify
```

This runs dependency checks, the full test suite, Ruff lint and Ruff format checks.

## Installation

Install with the Asana and Taskwarrior extras:

```sh
pip3 install syncall[asana,tw]
```

## See also

- `docs/taskwarrior-asana-project.md`
- `docs/taskwarrior-asana-operations.md`
- `docs/asana-rich-text-sync-safety.md`
- `docs/taskwarrior-filtering.md`
