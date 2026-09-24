# Asana ↔ Taskwarrior rich-text sync safety and future design

## Purpose

For the wider project context and operational runbook, also read:

- `docs/taskwarrior-asana-project.md`
- `docs/taskwarrior-asana-operations.md`

This document records the safety constraints and intended future design for Asana descriptions/notes and comments/Taskwarrior annotations.

It exists because Taskwarrior cannot faithfully represent all of Asana's rich-text structure. A naive "two-way sync" implementation can silently destroy information in Asana.

**Future agents must read this document before changing description, note, comment or annotation synchronisation.**

This note describes the state of the repository after the September 2026 safety work, including these commits:

- `e535a7fe36d9c93489263b7c0c77c41453bfc178` – preserve Asana comment rich-text metadata
- `e742323dea48e0852f22e9a95c4db59f75bfa138` – protect existing Asana rich text from lossy Taskwarrior round trips
- `4da02b6e114299964bd878997ce44312550d1c44` – safer Taskwarrior rich-text projection and image references
- `c7d8d3ba903852066406638c594c97103c468537` – tests proving rich descriptions/comments are protected
- `36f4fb2ad9ac95314b8770a2e52c3e9cc62d7441` – comment cache schema v3 for rich-text metadata

The current master branch may evolve, so inspect the live code as well as this document.

## Current behaviour and why it is safe

### Existing Asana tasks

For an existing Asana task:

- Asana `html_notes` is treated as canonical rich text.
- Asana → Taskwarrior converts the rich description into a readable Markdown-style projection stored in Taskwarrior `notes`.
- Taskwarrior → Asana updates deliberately **do not write `html_notes`**.
- Asana comments are read into Taskwarrior annotations.
- Genuinely new Taskwarrior annotations may be appended as new Asana comments through a dedicated append-only pathway.
- Taskwarrior still cannot edit or delete an existing Asana comment.

The key protection is currently in `AsanaSide.update_item()`:

```python
# Existing Asana rich text is canonical...
raw_task.pop("html_notes", None)
```

The existing-task `update_item()` path still does not call the historical missing-comment creation logic. Outbound comments are handled separately from whole-task conflict resolution so a new TW annotation is not lost merely because Asana wins an unrelated task conflict.

These protections are intentional. They must not be removed simply because conversions between Markdown and Asana HTML exist.

### New Taskwarrior → Asana tasks

New task creation is different.

For a genuinely new Taskwarrior task:

- Taskwarrior notes can be converted into initial Asana `html_notes` during task creation.
- The new Asana task identity is checkpointed first.
- Initial Taskwarrior annotations can then be created as Asana comments in `post_create_sync()`.
- Crash-recovery state prevents comment creation from being repeated blindly if creation is interrupted.

This path is acceptable because there is no pre-existing Asana rich document to destroy.

## Why a normal round trip is unsafe

Taskwarrior stores a useful projection, not a lossless serialisation of Asana rich text.

Asana rich descriptions/comments may contain data that the Taskwarrior representation cannot fully carry, including:

- embedded images and asset metadata
- user/task/project mentions and Asana-specific metadata
- rich links and metadata beyond ordinary Markdown links
- block quotes and richer block structure
- formatting and XML/HTML structure not represented by the projection
- future Asana rich-text elements unknown to syncall

The projection code has been improved so, for example, an Asana image can remain visible in Taskwarrior as a link to the Asana asset. That does **not** make the projection reversible.

Therefore this is unsafe:

```text
Asana HTML
  -> Taskwarrior Markdown projection
  -> regenerate Asana HTML
  -> overwrite original Asana HTML
```

The final document may look superficially similar while silently losing metadata or structure.

## Current append-only outbound comment behaviour

Outbound comments are designed to be self-healing and independent of syncall's disposable caches.

For every mapped task during a normal sync:

- syncall fetches the **live Asana comment history** before normal change detection
- those live comments replace any cached Asana comment snapshot, so a new Asana comment can itself trigger Asana → Taskwarrior synchronisation
- Taskwarrior stores a compact versioned `asana_comment_state` UDA on the task
- that ledger maps stable Asana comment GIDs to the corresponding Taskwarrior annotation identity (creation timestamp plus a compact text digest)
- if the ledger is missing, malformed or incomplete, syncall automatically rebuilds as much of it as possible from live Asana GIDs, `created_at` values and normalized comment text
- annotations that can be matched to existing Asana comments are treated as already synchronized
- an unmatched Taskwarrior annotation with a stable creation timestamp is treated as a missing outbound comment and is appended to Asana
- after appending, syncall fetches live Asana history again and immediately rebuilds the ledger using the real newly-created Asana GID
- existing Asana comments remain immutable from Taskwarrior; editing/deleting a TW copy never edits/deletes the Asana comment
- deleted Asana comment identities are retained as tombstones in the ledger so a lingering local copy cannot later be resurrected as a new comment
- live Asana duplicate checks still run immediately before every write
- identical-text comments are paired one-to-one, preferring the annotation timestamp closest to the Asana comment's creation time

This means the comment system does **not** rely on a one-time global baseline or the last serdes snapshot. A missed local annotation can be discovered and backfilled on a later ordinary sync even when no other field changed. Deleting syncall's local serdes/comment cache/preferences does not erase comment identity because task identity and comment identity are persisted in Taskwarrior and are cross-checked against live Asana state.

The remaining irreducible ambiguity is if **all durable identity is deliberately stripped from Taskwarrior as well** (for example the task is exported/imported without its `asana_gid` and `asana_comment_state`) *and* historical comments have been edited into indistinguishable forms. In normal syncall operation, cache loss alone does not create that condition.

## Desired future behaviour

The intended eventual behaviour is:

### Notes/descriptions

- genuine two-way editing
- Asana-only rich structures preserved
- Taskwarrior edits applied without replacing unrelated rich structures
- no silent winner when both sides independently changed
- safe migration for tasks that already diverged before the feature existed

### Comments/annotations

Editing comments from Taskwarrior is **not required**.

Future behaviour should be:

- Asana → Taskwarrior continues to reflect comments
- a genuinely new Taskwarrior annotation may append one new Asana comment
- an existing Asana comment is never edited or deleted because its Taskwarrior annotation changed or disappeared
- duplicate comments are not created

## Required architecture for two-way notes

### 1. Use field-level sync state, not whole-task recency

The current generic synchroniser can resolve whole-task conflicts using task modification timestamps. That is insufficient for notes.

A change to a due date, task name, follower, comment or another Asana field may change the task's `modified_at` without meaning the description changed.

Two-way notes therefore need their own durable, versioned sync state.

At minimum the baseline must let the implementation independently answer:

- has the canonical Asana rich description changed since the notes baseline?
- has the Taskwarrior notes field changed since the notes baseline?

Do not assume existing generic serdes snapshots are automatically a suitable notes baseline. Prove and version any state used for this purpose.

### 2. Preserve both representations in the baseline

Because the two sides have different representations, a useful baseline should preserve enough information to compare each side against its own last-synchronised form, for example:

- canonical/base Asana `html_notes`
- base Taskwarrior `notes` projection
- schema/version metadata
- stable task identities

Hashes may be stored for efficient comparison, but do not retain only a hash if the previous content is required to construct or diagnose a safe merge.

### 3. Classify each sync before writing

For each mapped task, notes synchronisation should distinguish at least:

**Neither side changed**
- Do nothing.

**Only Asana changed**
- Preserve the Asana rich document as canonical.
- Re-project it into Taskwarrior notes.
- Update the notes baseline only after the target write/read-back succeeds.

**Only Taskwarrior changed**
- Apply the Taskwarrior edit to the existing Asana rich document without destroying unsupported Asana nodes/metadata.
- Do **not** blindly run the entire Taskwarrior Markdown through `markdown_to_asana_html()` and replace `html_notes`.
- If the edit cannot be mapped onto the rich document safely, stop and report a conflict instead of degrading the Asana content.
- Update the baseline only after a successful Asana write and read-back.

**Both sides changed**
- Treat this as a notes conflict.
- Do not choose a winner automatically.
- Do not use whole-task `modified_at` recency as a substitute for a merge.
- Preserve both versions and surface enough information for a human to reconcile them.

"Fail closed" is preferable to any silent overwrite.

### 4. The Taskwarrior edit must be applied as a safe delta/merge

The important design constraint is that Taskwarrior should describe an edit to the representable content, not become a replacement serialisation of the Asana document.

A future implementation may use a structured diff, token mapping, rich-text AST or another approach. The exact mechanism can change, but it must preserve Asana nodes and attributes that Taskwarrior did not edit and cannot represent.

Examples that must survive an unrelated Taskwarrior text edit include:

- an embedded Asana image
- a true Asana mention
- an existing rich link
- quote/block structure
- unsupported/future Asana tags or metadata

If preservation cannot be guaranteed for a particular document/edit, produce a conflict rather than writing a lossy result.

## Migration when two-way notes is first enabled

This is critical because users may already have edited Asana notes and Taskwarrior notes separately while outbound note writes were disabled.

The first version that enables two-way notes must **not** treat the current Taskwarrior notes as automatically authoritative.

For every already-linked task:

1. Fetch the current canonical Asana rich notes.
2. Compute the current normal Asana → Taskwarrior projection.
3. Compare that projection with the current Taskwarrior notes.

If they match:
- there is no known local divergence
- establish the current pair as the initial notes baseline

If they differ:
- mark the task as an **initial notes conflict/divergence**
- write to neither side
- preserve both versions
- require reconciliation or an explicit migration choice before normal two-way notes sync begins for that task

This migration means the tool can be used safely today even if some notes later diverge. Divergence becomes a finite reconciliation problem, not a reason to overwrite one side.

Do not "solve" migration by choosing newest task `modified_at`. That timestamp is not notes-specific.

## Required architecture for comments

### Existing metadata

`AsanaComment` retains:

- `gid`
- plain `text`
- `created_at`
- canonical `html_text`

The Asana comment cache is versioned and currently stores rich comment metadata.

When projected into Taskwarrior, imported annotations can carry source identity/time through `SyncAnnotation`.

These identities are important and should be preserved.

### Outbound behaviour is append-only

For existing tasks:

- an annotation that came from an Asana comment must never be re-posted as a new comment
- editing the Taskwarrior annotation must not edit the Asana comment
- deleting the Taskwarrior annotation must not delete the Asana comment
- editing/deleting an Asana comment may be reflected back into Taskwarrior using its stable source identity
- only a genuinely new local annotation should be eligible to create a new Asana comment

### Comment state migration and recovery

There is no separate migration command and no global one-time baseline. Each normal sync reconciles the per-task durable ledger against live Asana history. Tasks without a ledger are automatically bootstrapped; incomplete ledgers are automatically repaired; unmatched local annotations are automatically backfilled. For a newly created Taskwarrior → Asana task, the existing post-create pathway remains a separate, valid case.

### Duplicate prevention

Before creating a comment:

- prefer stable identity/state over text-only matching
- use live remote checks when needed
- preserve crash-recovery semantics
- never clear pending/recovery state until remote existence is confirmed

Text normalisation may be a secondary duplicate guard, but it must not replace stable identity.

## Code areas that future work must review

At minimum inspect:

- `syncall/asana/asana_side.py`
  - `update_item()`
  - comment fetch/cache logic
  - `ensure_comments()`
  - `post_create_sync()`
- `syncall/asana/asana_task.py`
  - `AsanaComment`
  - `AsanaTask.html_notes`
- `syncall/asana/rich_text.py`
  - Asana HTML ↔ Markdown projection
- `syncall/tw_asana_utils.py`
  - `convert_tw_to_asana()`
  - `convert_asana_to_tw()`
- `syncall/taskwarrior/taskwarrior_side.py`
  - annotation handling
  - Asana identity/recovery fields
- `syncall/aggregator.py`
  - change detection
  - update read-back
  - serdes/baseline behaviour
  - post-create identity checkpointing
- `syncall/scripts/tw_asana_sync.py`
  - recovery and orchestration

Do not assume a future refactor preserves the same filenames; search for the equivalent behaviour.

## Minimum regression tests before enabling outbound notes

A future implementation must add tests covering at least:

1. Unchanged Taskwarrior notes never cause an Asana rich-text write.
2. An Asana-only notes edit projects safely into Taskwarrior.
3. A Taskwarrior-only plain-text edit updates the intended Asana text while preserving an embedded image.
4. A Taskwarrior-only edit preserves a true Asana mention and its metadata.
5. A Taskwarrior-only edit preserves existing rich links.
6. A Taskwarrior-only edit preserves quote/block structure that is not being edited.
7. Unsupported/future Asana markup is preserved or causes a safe conflict, never silent deletion.
8. Independent edits on both sides produce a conflict and no writes.
9. Initial migration with identical TW projection establishes a baseline without writes.
10. Initial migration with divergent notes produces a conflict without writes.
11. A failed outbound note write does not advance the notes baseline.
12. A successful outbound note write is read back before the baseline is committed.
13. A rerun after success is a no-op.

Use realistic rich-text fixtures, not only plain strings.

## Minimum regression tests before enabling outbound comments

Add tests covering at least:

1. Existing Asana-derived annotations are never re-posted.
2. Editing an imported Taskwarrior annotation does not edit/recreate the Asana comment.
3. Deleting an imported Taskwarrior annotation does not delete the Asana comment.
4. One new post-baseline Taskwarrior annotation creates exactly one Asana comment.
5. A rerun does not duplicate that comment.
6. Pre-feature local annotations are not bulk-published during migration.
7. Rich Asana comment metadata remains preserved in cache/state.
8. Crash during comment creation is safely recoverable without duplicate publication.
9. New-task post-create comment behaviour remains safe.

## Rollout guidance

Prefer a staged implementation:

1. Add/version the notes/comment-specific baseline state without changing write behaviour.
2. Add migration detection and a dry-run/conflict report.
3. Add the full regression suite.
4. Implement safe notes merge/apply logic.
5. Enable outbound notes only after migration has established baselines.
6. Keep the existing append-only outbound comments separate from notes merging.
7. Keep an easy fail-closed switch that disables outbound rich-text/comment writes while leaving Asana → Taskwarrior projection working.

Do not combine "remove the current guard" and "invent the new merge algorithm" into one unobservable change.

## If a bug or suspected data loss occurs

Immediately prefer preservation over convergence:

- disable outbound Asana rich-text/comment writes
- do not delete caches/baselines before inspecting them
- fetch and preserve the current Asana rich content
- preserve the current Taskwarrior notes/annotations
- audit the affected sync window and task identities
- only resume outbound writes after the overwrite path is understood and regression-tested

The safe fallback is the current September 2026 model: Asana rich text/comments are canonical for existing tasks and Taskwarrior receives a projection.

## Bottom line

The current one-way protection is intentionally conservative and safe.

The desired future end state is two-way **notes** plus append-only outbound **comments**, but it must be implemented as field-specific stateful merging with explicit migration and conflict handling.

Never restore two-way behaviour by merely deleting the existing protections.
