# Taskwarrior ↔ Asana work-sync project

_Last updated: 24 September 2026_

## Purpose

This fork of `syncall` is being developed into a dependable personal work-sync layer between Asana and Taskwarrior.

The goal is **not** to make the two products identical. The intended roles are different:

- **Asana** is the operational/day-to-day source used with colleagues, clients and shared work.
- **Taskwarrior** is the local CLI mirror used for personal planning, querying, reporting, prioritisation and analysis.
- syncall keeps the overlapping task data aligned while preserving information that only one side can represent.

The governing principle is:

> Prefer preserving user data over forcing convergence.

If syncall cannot prove that a write is safe, it should fail closed, report the ambiguity and leave the remote data untouched.

## Current deployment model

The main personal profile is the saved `work` combination.

Typical characteristics:

- Taskwarrior sync scope is the `+asana` population.
- Asana discovery includes tasks assigned to the authenticated user **and** follower-only tasks, across both complete and incomplete work.
- The working dataset is large: roughly 2,700–2,800 mapped tasks, so whole-workspace operations must remain efficient and visibly progressive.
- The current whole-task conflict strategy is `MostRecentRS`, but field-specific features such as notes/comments must not blindly inherit whole-task recency semantics.
- Taskwarrior tasks without `+asana` are personal/local and must not be pushed into Asana by the work sync.

Normal local run:

```sh
./scripts/sync-work
```

Repository verification:

```sh
./scripts/update-verify
```

The wrapper loads `.env` automatically. Do not ask the user to paste the Asana PAT into chat or source code.

## Product requirements

### 1. Safe bidirectional task sync

The sync should preserve the useful overlap between both systems:

| Asana | Taskwarrior | Direction / rule |
| --- | --- | --- |
| task GID | `asana_gid` | durable identity |
| `[Client] Task name` | `client:Client` + clean `description` | two-way |
| plain task name | `description` | two-way |
| `completed` | `status` | two-way |
| `created_at` | `entry` | preserve historical source date |
| `completed_at` | `end` | preserve historical source date |
| `modified_at` | `modified` | used for generic change/conflict logic |
| `due_at` / `due_on` | `due` | two-way with local-calendar safety |
| `html_notes` | `notes` | currently Asana → TW projection only for existing tasks |
| comments | annotations | Asana → TW, plus append-only genuinely new TW annotations → Asana |

Historical dates matter. Imports should not turn old Asana tasks/comments into apparently new Taskwarrior activity merely because the sync ran today.

### 2. Asana rich text is canonical for existing tasks

Taskwarrior cannot faithfully represent all Asana rich text.

Asana descriptions/comments can contain:

- embedded images and Asana asset metadata
- user/task/project mentions
- rich links
- underlining, emphasis and block structure
- quotes and lists
- Asana-specific attributes
- future rich-text nodes unknown to syncall

Therefore, for an **existing Asana task**, the current safe behaviour is:

- Asana `html_notes` → readable Taskwarrior `notes` projection.
- Taskwarrior `notes` must **not** replace existing Asana `html_notes`.
- Existing Asana comments must never be edited or deleted from Taskwarrior.
- New Taskwarrior → Asana task creation is a separate case and may create initial notes/comments because there is no pre-existing Asana rich document to destroy.

See `docs/asana-rich-text-sync-safety.md` for the full design and future two-way notes plan.

### 3. Comments are append-only from Taskwarrior

The desired behaviour for comments is intentionally narrower than normal two-way text sync:

- Existing Asana comments project into Taskwarrior annotations.
- Imported annotations retain stable identity/time metadata where possible.
- A **genuinely new** local Taskwarrior annotation may append one new Asana comment.
- Editing an imported Taskwarrior annotation must not edit or recreate the Asana comment.
- Deleting an imported annotation must not delete the Asana comment.
- Deleted remote comment identities must not later be resurrected as new comments.
- Reruns must not create duplicates.

The durable per-task Taskwarrior UDA `asana_comment_state` stores comment identity bindings. Normal sync reconciles that state against live Asana history and repairs missing/incomplete state automatically.

The Asana comment cache is a **performance optimisation only**. Correctness must not depend on it.

### 4. Durable identity and recoverability

Mapped task identity must survive ordinary cache/config loss.

Current mechanisms include:

- `asana_gid` stored on Taskwarrior tasks.
- the normal syncall correspondence map.
- `asana_comment_state` for durable comment identity.
- `asana_pending_comments` for crash recovery during new TW → Asana task creation.
- live Asana rechecks before outbound comment writes.

If the correspondence YAML disappears, mappings should be recoverable from Taskwarrior where possible.

If a local sync snapshot is missing for a mapped item, **baseline the current state rather than guessing a direction and writing remote data**.

Conflicting identity recovery must abort instead of guessing.

### 5. Deletion and scope safety

A task disappearing from a filtered result is **not enough evidence that it was deleted**.

Required behaviour:

- Verify mapped missing Asana tasks directly before propagating deletion.
- A genuine Asana deletion may propagate to Taskwarrior.
- Falling out of assignment/follower/filter scope must not become a deletion.
- Forbidden/inaccessible is not equivalent to Not Found.
- Future filtering rules must detach/ignore excluded tasks without deleting Asana data.
- An excluded task must not be automatically re-imported while excluded.
- If the rule later makes it eligible again, normal import may resume.

Do not implement exclusion as a manual GID blacklist.

### 6. No-op writes are unacceptable

A sync should not issue remote writes merely because its serialized representation differs internally.

Required behaviour:

- compare semantic fields before writing
- send only genuinely changed Asana fields
- after any successful target write, read back the **actual stored target state**
- checkpoint that actual state, not merely the intended payload
- failed writes must not advance source sync state
- a rerun after a successful sync should converge to a no-op

This is both a correctness and auditability requirement: even a harmless same-value Asana PUT advances `modified_at` and creates noise.

### 7. Transactional sync state

The source snapshot must not be marked "seen" before the corresponding target write succeeds.

A partial failure should leave enough authoritative state for the next run to retry only the outstanding work.

The correspondence map should still be flushed when identity changes were successfully established, even if a later operation fails.

### 8. Local-time semantics

The user's working timezone is UK local time (`Europe/London` semantics).

Taskwarrior internally resolves date/time values to UTC epoch/serialized UTC, which is fine. User-facing dates should still behave as local dates/times.

Particular rule:

- an Asana date-only `due_on` represents a **calendar day**
- converting it through Taskwarrior UTC must never shift it to the previous/next Asana day
- before extracting `due_on` from a timezone-aware Taskwarrior value, convert it back to the local timezone

There are regression tests for the BST case where local midnight is the previous UTC calendar day.

### 9. Visible progress, not silent blocking

Operations across ~2,700+ tasks can take minutes.

Any potentially long stage should provide visible Rich progress/status UI, including:

- Taskwarrior snapshot loading
- Asana discovery
- Asana history/comment loading
- change detection
- identity recovery/backfill
- comment reconciliation/recovery
- applying sync operations
- saving sync state
- annotation timestamp reconciliation

Avoid adding queues, async worker systems or architectural complexity just to improve progress display. Presentation-only progress around the existing synchronous design is preferred.

Progress totals must account correctly for skipped conversions; a deliberately skipped item must not leave a progress bar permanently below 100%.

## Current safe architecture

### Task identity

`asana_gid` is an internal Taskwarrior UDA injected by syncall.

Users do not need to manually configure it for syncall to operate.

### Comment identity

`asana_comment_state` is an internal versioned Taskwarrior UDA. It binds Asana comment GIDs to the local annotation identity used for reconciliation.

Normal sync should self-heal this state from live Asana history.

### Pending creation recovery

For new TW → Asana tasks:

1. Create the Asana task without comments.
2. Durably checkpoint identity.
3. Store the Taskwarrior `asana_gid` and pending-comment marker.
4. Create/reconcile comments.
5. Clear pending state only after remote existence is confirmed.

This order is deliberate. Never move comment creation before durable identity checkpointing.

### Caches

Caches should improve speed, not determine truth.

In particular:

- deleting/corrupting `asana_comments.json` must not create duplicate comments
- cached comments never authorize an outbound comment write
- live Asana state is rechecked when a write decision needs remote truth

### Process concurrency

A local process lock prevents two `sync-work` runs on the same machine racing each other.

## Filtering roadmap

The desired future filter system is broader than the current assigned/follower discovery.

It should support arbitrary Asana metadata predicates, for example:

- project membership
- assignee
- no owner / unassigned
- other task metadata that proves useful

Rules must be evaluated automatically every sync.

When a mapped task becomes excluded:

- stop syncing it
- do not delete it from Asana
- do not misclassify it as a remote deletion
- local Taskwarrior cleanup/detach may be permitted
- do not re-import while the exclusion remains true

If the rule later changes and the task becomes eligible again, it may re-enter normally.

Keep the design simple and rule-driven. Avoid permanent per-task blacklists unless there is a genuinely different use case.

## Auditability and forensics

### Sync change logs

Every work sync appends to two plain-text logs:

- `~/.config/syncall/logs/taskwarrior-to-asana.log`
- `~/.config/syncall/logs/asana-to-taskwarrior.log`

The run prints both paths. Each record has a run id, task name, Taskwarrior uuid, Asana gid, an Asana link when the gid is known, operation, success or failure, and the before/after value of every changed field, including comments and annotations. Local identity bookkeeping is labeled as such. A run that writes nothing records that explicitly.

These logs are an observability aid. They are not a source of sync truth, and a missing or unreadable log must not cause sync to guess a write.

### Existing read-only audit tool

`./scripts/audit-asana-window` is the forensic tool for reconstructing Asana activity over a time window.

It:

- loads the existing PAT from `.env`
- performs read-only Asana API requests
- discovers candidate tasks modified since the window start
- filters historical task stories by `story.created_at` within the requested window
- retains a `current_modified_at` fallback because some writes may move `modified_at` without a visible story
- writes task CSV, story CSV and JSON output

See `docs/taskwarrior-asana-operations.md` for exact commands and timezone examples.

### Required future write journal

The directional change logs above are the current review record. A later journal may still move to structured JSONL if a more mechanical forensic format becomes useful. Any such journal remains observability only.

## September 2026 incident and lessons

A migration/safety run exposed an important feedback loop.

Final forensic classification of the affected Asana run:

- **32 successful Asana writes**
- **27** pointless name-only writes
- **3** lossy description rewrites
- **2** due dates shifted back one calendar day
- **0** comment changes
- **0** completion changes
- **0** assignment changes
- one additional attempted update failed before successful completion

The user manually repaired the real Asana changes.

Root causes/lessons included:

1. sync-generated Taskwarrior changes could later be detected as fresh local changes
2. successful writes had cached intended values rather than always reading back actual stored target state
3. the Asana update path could send unchanged fields
4. rich Asana descriptions were being round-tripped through a lossy Markdown representation
5. date-only values could be interpreted using UTC calendar date rather than UK local calendar date

The fixes established requirements that must not regress:

- reload Taskwarrior state after writes
- cache actual target read-back
- suppress no-op Asana fields/PUTs
- never rewrite existing Asana rich descriptions from TW projection
- preserve UK-local calendar semantics for date-only due dates
- maintain read-only forensic tooling

This incident is why "looks equivalent" is not an adequate safety standard.

## Rich-text roadmap

The desired eventual end state is genuine two-way **notes**, but not by making Taskwarrior Markdown canonical.

Future two-way notes need field-specific versioned state and three-way classification:

- neither side changed → no-op
- Asana only changed → re-project into TW
- TW only changed → apply a safe delta to the existing Asana rich document while preserving unsupported nodes
- both changed → conflict, no automatic winner

Initial migration must detect already-divergent notes and fail closed.

See `docs/asana-rich-text-sync-safety.md` for the detailed merge/migration/test specification.

## Taskwarrior reporting and local workflow goals

Taskwarrior is valuable here partly because it enables local analysis that Asana does not provide as cleanly. It is a mirror: reporting is only current as of the last successful sync.

Desired/ongoing reporting includes:

- backlog size/trend over time
- tasks added vs completed/deleted
- net backlog growth and net tasks/day
- simple client + description operational reports
- personal priority/next-up workflows
- time-estimate planning

Local-only planning fields such as original estimate / remaining estimate (currently conceived as `est` / `rem`) should remain local unless an explicit Asana mapping is deliberately designed later.

Bulk Taskwarrior enrichment may use Taskwarrior JSON export/import workflows. That is separate from the Asana sync contract.

## Non-goals / design preferences

- Do not turn syncall into a distributed service.
- Do not add background workers/queues merely for speed or UI.
- Do not make caches authoritative.
- Do not use whole-task recency to resolve field-specific rich-text conflicts.
- Do not weaken safety to achieve "perfect" symmetry between the products.
- Do not silently discard unsupported Asana content.
- Do not produce remote writes just to normalize formatting.
- Do not require manual recovery scripts for normal comment-state repair.
- Do not require the user to maintain lists of Asana GIDs for filtering.

Prefer simple deterministic code, live verification at dangerous boundaries, durable identity, explicit failure and strong tests.

## Development/verification expectations

Before changing behaviour:

1. inspect the current live branch; do not rely on an old chat description
2. identify the exact persistence/write path involved
3. consider cache loss, restart, partial failure and retry behaviour
4. add regression tests for the actual failure mode
5. run the full verification suite

The expected local verification entrypoint is:

```sh
./scripts/update-verify
```

It performs dependency checks, the full pytest suite, Ruff lint and Ruff formatting checks.

Do not leave the user in a repeated Ruff/test feedback loop. A change should be considered ready only after tests, lint and formatting are clean.

## Related documentation

- `docs/readme-tw-asana.md` — integration-level usage and current behaviour
- `docs/taskwarrior-asana-operations.md` — local runbook, audit tooling and incident procedure
- `docs/asana-rich-text-sync-safety.md` — detailed rich-text/comment safety and future merge design
- `docs/taskwarrior-filtering.md` — general Taskwarrior filter documentation
