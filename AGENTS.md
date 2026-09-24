# Agent instructions

## Read this before changing Taskwarrior ↔ Asana behaviour

This repository contains a heavily customised Taskwarrior ↔ Asana sync used as a personal work system.

Before changing that integration, read these in order:

1. `docs/taskwarrior-asana-project.md` — project purpose, current architecture, invariants and roadmap
2. `docs/taskwarrior-asana-operations.md` — local runbook, audit tooling, timezone behaviour and incident procedure
3. `docs/asana-rich-text-sync-safety.md` — detailed rich-text/comment safety design

Do not rely on an old chat summary when the live code/docs differ. Inspect current `master` first.

## Project intent

Asana is the shared operational system. Taskwarrior is the local planning/reporting mirror.

The aim is useful bidirectional sync where the data models overlap, **not** perfect symmetry between the products.

The governing rule is:

> Prefer preserving user data over forcing convergence.

If a write cannot be proved safe, fail closed and leave the remote data untouched.

## Non-negotiable safety invariants

### Existing Asana rich text is canonical

For an **existing Asana task**:

- Taskwarrior must not wholesale replace Asana `html_notes` from its Markdown projection.
- Embedded images, links, mentions, formatting and Asana-specific rich metadata must survive unrelated Taskwarrior edits.
- Whole-task `modified_at` recency is not sufficient to resolve a notes conflict.
- If future two-way notes cannot safely map a local edit onto the rich Asana document, surface a conflict rather than degrading the document.

The current guard in `AsanaSide.update_item()` deliberately excludes `html_notes` from existing-task updates. Do not remove it as a shortcut.

### Existing Asana comments are immutable from Taskwarrior

- Asana comments may project to Taskwarrior annotations.
- A genuinely new local Taskwarrior annotation may append one new Asana comment.
- Editing/deleting an imported annotation must not edit/delete/recreate the existing Asana comment.
- Outbound comment support must remain append-only, identity-aware and live-rechecked.
- Durable comment identity lives in the Taskwarrior `asana_comment_state` UDA and must self-heal from live Asana history.
- The Asana comment cache is performance-only and must never authorize a write.

### Identity must be durable

- Mapped tasks carry `asana_gid` in Taskwarrior.
- New TW → Asana creation checkpoints identity before comments.
- `asana_pending_comments` is crash-recovery state.
- Missing correspondence/snapshot state must be recovered or safely baselined; never guess a destructive direction.
- Conflicting recovered mappings abort rather than guess.

### Scope changes are not deletions

A mapped task disappearing from a filtered result does not prove deletion.

Verify remotely before propagating deletion. A task that merely becomes unassigned, unfollowed or excluded by future filter rules must not be deleted from Asana.

### No-op remote writes are bugs

Do not send Asana updates merely to normalize representations.

- send only genuinely changed fields
- read back actual target state after writes
- checkpoint actual stored state, not intended payload
- failed writes must not commit the source snapshot
- rerunning a successful no-change sync should converge to a no-op

### Date-only values use local calendar semantics

The user's work timezone is UK local time (`Europe/London` semantics).

Taskwarrior may serialize timestamps in UTC, but an Asana `due_on` is a local calendar date. Always convert timezone-aware TW values back to local time before deriving a date-only Asana value. Regression tests cover the BST previous-UTC-day case.

## Comment reconciliation requirements

Do not infer outbound comments from serdes/cache alone.

For each mapped task, normal sync should reconcile:

- live Asana comment GIDs
- Asana comment `created_at`
- normalized text as a secondary guard
- durable Taskwarrior `asana_comment_state`
- Taskwarrior annotation timestamps

Requirements:

- one local annotation creates at most one remote comment
- a retry after an ambiguous POST must not duplicate it
- deleted remote identities remain tombstoned so stale local copies are not resurrected
- cache/preferences loss alone must not erase identity
- normal sync must repair/backfill incomplete state automatically; no special repair script should be required

## New TW → Asana task creation

This is a separate safe path because no pre-existing Asana rich document exists.

Required order:

1. create the Asana task without comments
2. checkpoint correspondence identity
3. persist `asana_gid` and pending-comment recovery state
4. append/reconcile comments
5. confirm remote existence
6. clear pending state

Do not create comments before identity checkpointing.

## Failure and state semantics

The sync should behave transactionally enough that a failed write does not make the next run believe the source change already succeeded.

- source snapshots commit only after successful operations
- successful target writes are read back before checkpointing
- partial target writes should checkpoint the live target best-effort without masking the original error
- correspondence changes that were successfully established should still be flushed

## Filtering roadmap

Future filtering should support rule-driven Asana metadata predicates such as project/assignee/unassigned state.

When a mapped task becomes excluded:

- detach/ignore it
- leave Asana untouched
- do not treat it as deleted
- do not re-import while excluded
- allow normal re-entry if rules later make it eligible again

Do not implement this as a permanent manual GID blacklist.

## UI expectations

The sync operates over roughly 2,700–2,800 tasks and can take minutes.

Any potentially slow stage should have visible Rich status/progress. Prefer presentation-only progress around the current synchronous design; do not add queues/background workers simply for UI.

Progress totals must reach 100% even when conversions are deliberately skipped.

## Auditability

`./scripts/audit-asana-window` is the current read-only forensic tool.

Each `./scripts/sync-work` run also appends plain-text change logs under `~/.config/syncall/logs/`:

- `taskwarrior-to-asana.log` records creates, updates, deletes, and comments written to Asana
- `asana-to-taskwarrior.log` records the corresponding Taskwarrior writes, including local identity bookkeeping

The logs include run id, task ids, operation, result, and before/after field values. They are not sync truth.

Do not use audit logs as sync truth.

## September 2026 incident

The corrected final forensic classification was:

- 32 successful Asana writes
- 27 pointless name-only writes
- 3 lossy description rewrites
- 2 due dates shifted back one day
- no comment/completion/assignment changes
- one additional attempted update failed

The user manually repaired the real Asana changes.

The incident established several permanent requirements:

- reload Taskwarrior state after writes
- cache actual target read-back
- suppress no-op Asana updates
- protect existing Asana rich descriptions
- preserve UK-local date semantics
- retain forensic tooling

Do not reintroduce the behaviours that caused this incident.

## Development workflow

The user wants simple, deterministic architecture and does not want repeated test/Ruff back-and-forth.

Before considering a behavioural change ready:

1. inspect current code and state model
2. consider restart/cache-loss/partial-failure/retry behaviour
3. add realistic regression tests
4. run the full verification entrypoint:

```sh
./scripts/update-verify
```

This must pass:

- dependency checks
- full pytest suite
- Ruff lint
- Ruff format check

Do not claim verification passed unless it actually ran and passed.

Do not casually delete persistent syncall/Taskwarrior state while troubleshooting.

## Useful commands

Normal work sync:

```sh
./scripts/sync-work
```

Read-only Asana activity audit:

```sh
./scripts/audit-asana-window --help
```

Full local verification:

```sh
./scripts/update-verify
```

## Bottom line

Protect Asana data first. Keep identity durable. Keep comments append-only. Treat rich notes as a field-specific merge problem, not a whole-task recency problem. Avoid no-op writes. Fail closed on ambiguity. Keep the implementation observable, testable and simple.
