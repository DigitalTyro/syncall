# [Taskwarrior](https://taskwarrior.org/) ⬄ [Asana](https://www.asana.com)

## Description

Synchronize Asana tasks.

Upon execution, `tw_asana_sync` will synchronize, and on subsequent runs keep
synchronized, the following attributes:

- Asana task `name` <-> TW task `description`
- An Asana name such as `[Client] Task name` <-> TW `client:Client` plus
  `description:Task name`
- Asana task `html_notes` <-> TW `notes`, using Markdown on the Taskwarrior side
- Asana comments <-> TW annotations
- Asana task `completed` <-> TW task `status`
- Asana task `due_at` or `due_on` <-> TW task `due`
- Asana task is deleted <-> TW task is deleted

The workspace sync includes tasks assigned to the authenticated user as well as
tasks where that user is a follower. Follower-only discovery uses Asana workspace
search and manually pages by task creation time so searches larger than 100
results are not silently truncated.

Rich task descriptions are represented as Markdown in Taskwarrior and converted
to Asana's supported rich-text HTML when written back. If the Markdown
representation has not changed, syncall leaves the existing Asana `html_notes`
untouched so Asana-only metadata such as true @mentions is preserved.

Comment synchronization is deliberately conservative. Existing Asana comments
are imported as Taskwarrior annotations and new Taskwarrior annotations are
appended to Asana as comments. Removing an annotation does not delete the
corresponding Asana comment.

Current limitations:

- Does not sync Asana tags, project names, subtasks, projects, likes, etc.
- Does not sync Taskwarrior tags or projects.
- Only supports authentication with an Asana [Personal Access Token](https://developers.asana.com/docs/personal-access-token).
- Follower-only task discovery requires access to Asana's premium workspace task
  search API.
- Editing an existing Taskwarrior annotation creates a new Asana comment rather
  than editing or deleting an existing Asana comment.

## Setup and Usage

You can synchronize a series of Taskwarrior tasks that have a particular
(or multiple) tags, synchronize all tasks that belong to a particular project,
or use `--sync-all-tw-tasks`.

Use `--taskwarrior-tags ...` or `--taskwarrior-project` respectively for the
first two approaches.

### Configure Taskwarrior fields

syncall supplies the `client` and `notes` UDA definitions while it is running.
To view and edit those fields directly with the normal `task` command, add them
to your persistent Taskwarrior configuration once:

```sh
task config uda.client.type string
task config uda.client.label Client
task config uda.notes.type string
task config uda.notes.label Notes
```

For example, a short multiline Markdown note can then be entered directly in the
terminal:

```sh
task 12 modify notes:"Check:
- UK
- US
- **Revenue**"
```

A Taskwarrior annotation becomes an Asana comment on the next sync:

```sh
task 12 annotate "Please review the final numbers"
```

### Authenticate

First, generate a [personal access token](https://developers.asana.com/docs/personal-access-token) on Asana.

| ![1](../misc/asana/authentication/1.png) | ![2](../misc/asana/authentication/2.png) | ![3](../misc/asana/authentication/3.png) |
| :--------------------------------------: | :--------------------------------------: | :--------------------------------------: |

Next, make this token available to `tw_asana_sync`. This can be done by either:

- Storing the token in environment variable `ASANA_PERSONAL_ACCESS_TOKEN`.
- Storing the token with [password store](https://wiki.archlinux.org/title/Pass),
  and telling Asana to load the token with `--token-pass-path`.

### Find IDs of available workspace

```sh
tw_asana_sync --list-asana-workspaces
```

Example output:

```text
Asana workspaces:
====================

- My Workspace: gid=123456789012345
```

### Synchronize workspace tasks

To synchronize Asana tasks within a workspace:

```sh
tw_asana_sync --taskwarrior-tags asana --asana-workspace-gid 123456789012345 --token-pass-path <path-to-asana-token-in-password-store>
```

Or:

```sh
tw_asana_sync --taskwarrior-tags asana --asana-workspace-name my-workspace --token-pass-path <path-to-asana-token-in-password-store>
```

### Pass the Access Token via environment variable

If you haven't installed or don't want to install [password
store](https://wiki.archlinux.org/title/Pass), you can pass the access token via an
environment variable:

```sh
ASANA_PERSONAL_ACCESS_TOKEN=123456789012345 tw_asana_sync -t asana -W my-workspace
```

## Installation

### Package Installation

Install the `syncall` package enabling the `asana` and `tw` extras:

```sh
pip3 install syncall[asana,tw]
```

## See also

- <a href="https://github.com/bergercookie/syncall/blob/master/docs/taskwarrior-filtering.md">Taskwarrior Filtering.md</a>.
