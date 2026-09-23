# Agent instructions

## Critical Asana ↔ Taskwarrior rich-text safety

Before changing any Asana description/note or comment/annotation sync behaviour, read:

- `docs/asana-rich-text-sync-safety.md`

This is a data-loss boundary, not a formatting preference.

### Current safety invariant

For an **existing Asana task**, Taskwarrior must not overwrite Asana `html_notes` or recreate/update Asana comments from Taskwarrior annotations.

The current guard in `syncall/asana/asana_side.py` deliberately removes `html_notes` from updates:

```python
raw_task.pop("html_notes", None)
```

Existing-task comment creation is also deliberately disabled. Do not remove either protection as a shortcut to "restore two-way sync".

Taskwarrior contains a readable but **lossy projection** of Asana rich text. A round trip can destroy or degrade Asana-only structure and metadata such as embedded images, mentions, rich links, quotes and formatting.

### Non-negotiable rules for future work

- Do not make Taskwarrior's projected Markdown the canonical representation of existing Asana rich text.
- Do not regenerate and replace an existing Asana description wholesale from Taskwarrior Markdown.
- Do not use whole-task `modified_at` timestamps alone to decide which notes version wins.
- Do not automatically resolve a task where both Asana notes and Taskwarrior notes have independently changed.
- Do not upload all Taskwarrior annotations that lack an Asana source ID when append-only comment sync is introduced; pre-existing local annotations need a migration/baseline.
- Existing Asana comments must remain immutable from Taskwarrior. Future outbound comment support should be append-only for genuinely new Taskwarrior annotations.
- New Taskwarrior → Asana task creation is a separate path and may continue to create initial notes/comments after identity has been checkpointed.
- Any future two-way notes implementation must fail closed on ambiguity and include the migration and regression tests described in the detailed safety document.

If a proposed change conflicts with these rules, stop and redesign it rather than weakening the guard.
