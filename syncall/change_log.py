"""Append-only logs of Taskwarrior ↔ Asana changes.

These files are for review after a sync. They are not read back by sync and must never
decide whether a later write is safe.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from bubop import logger
from xdg import xdg_config_home

if TYPE_CHECKING:
    from pathlib import Path

TO_ASANA = "taskwarrior-to-asana"
TO_TASKWARRIOR = "asana-to-taskwarrior"

ASANA_FIELDS = (
    "name",
    "completed",
    "completed_at",
    "due_on",
    "due_at",
    "html_notes",
)
TASKWARRIOR_FIELDS = (
    "description",
    "status",
    "client",
    "project",
    "due",
    "end",
    "scheduled",
    "notes",
    "tags",
    "asana_gid",
    "asana_pending_comments",
    "asana_comment_state",
)


@dataclass(frozen=True)
class TaskContext:
    """Identifies the task currently being written, for nested comment logs."""

    tw_uuid: str | None = None
    asana_gid: str | None = None
    task_name: str | None = None


@dataclass(frozen=True)
class FieldChange:
    """One scalar field that differs between stored before and after states."""

    name: str
    before: object
    after: object


@dataclass(frozen=True)
class TextChange:
    """A comment or annotation that was added, amended, or removed."""

    kind: str
    before: str | None = None
    after: str | None = None
    identity: str | None = None


class SyncChangeLog:
    """Write one append-only plain-text log for each sync direction."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or (xdg_config_home() / "syncall" / "logs")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid4().hex[:12]
        self.started_at = datetime.datetime.now().astimezone()
        self._paths = {
            TO_ASANA: self.directory / "taskwarrior-to-asana.log",
            TO_TASKWARRIOR: self.directory / "asana-to-taskwarrior.log",
        }
        self._counts: dict[str, Counter[str]] = {
            TO_ASANA: Counter(),
            TO_TASKWARRIOR: Counter(),
        }
        self._context = TaskContext()
        self._finished = False
        for direction, path in self._paths.items():
            self._append(path, self._header(direction), required=True)

    def path_for(self, direction: str) -> Path:
        return self._paths[direction]

    @contextmanager
    def task_context(
        self,
        *,
        tw_uuid: str | None = None,
        asana_gid: str | None = None,
        task_name: str | None = None,
    ):
        """Remember the task identity while a nested comment write is in progress."""
        previous = self._context
        self._context = TaskContext(
            tw_uuid=tw_uuid or previous.tw_uuid,
            asana_gid=asana_gid or previous.asana_gid,
            task_name=task_name or previous.task_name,
        )
        try:
            yield
        finally:
            self._context = previous

    def record(
        self,
        *,
        direction: str,
        operation: str,
        result: str,
        tw_uuid: str | None = None,
        asana_gid: str | None = None,
        task_name: str | None = None,
        fields: Sequence[FieldChange] = (),
        texts: Sequence[TextChange] = (),
        note: str | None = None,
        error: str | None = None,
    ) -> None:
        """Append one change record. Logging errors must not affect the sync."""
        if result == "succeeded" and operation == "updated" and not fields and not texts:
            return

        tw_uuid = tw_uuid or self._context.tw_uuid
        asana_gid = asana_gid or self._context.asana_gid
        task_name = task_name or self._context.task_name
        self._counts[direction][f"{operation} {result}"] += 1
        self._append(
            self._paths[direction],
            _format_record(
                run_id=self.run_id,
                operation=operation,
                result=result,
                tw_uuid=tw_uuid,
                asana_gid=asana_gid,
                task_name=task_name,
                fields=fields,
                texts=texts,
                note=note,
                error=error,
            ),
        )

    def finish(self, *, failed: bool) -> None:
        """Close the run in both logs. Safe to call once from a finally block."""
        if self._finished:
            return
        self._finished = True
        finished_at = datetime.datetime.now().astimezone()
        for direction, path in self._paths.items():
            self._append(path, _format_footer(self._counts[direction], finished_at, failed))

    def _header(self, direction: str) -> str:
        if direction == TO_ASANA:
            title = "Changes written to Asana from Taskwarrior"
        else:
            title = "Changes written to Taskwarrior from Asana"
        started = self.started_at.isoformat(timespec="seconds")
        return (
            "=" * 80 + "\n"
            f"{title}\n"
            f"Run {self.run_id} started {started}\n"
            "This log is append-only and is not used to decide later sync writes.\n"
            "Lines marked as identity or comment identity are local bookkeeping.\n"
            "A run with no change records below did not write task content.\n" + "=" * 80
        )

    def _append(self, path: Path, text: str, *, required: bool = False) -> None:
        payload = text if text.endswith("\n") else f"{text}\n"
        try:
            _append_record(path, payload)
        except Exception as exc:
            if required:
                raise RuntimeError(f"Could not write sync change log {path}.") from exc
            logger.opt(exception=True).warning(f"Could not append sync change log {path}.")


def diff_stored_items(
    before: object,
    after: object,
    *,
    asana: bool,
) -> tuple[list[FieldChange], list[TextChange]]:
    """Return stored field, comment, and annotation differences."""
    field_names = ASANA_FIELDS if asana else TASKWARRIOR_FIELDS
    fields = [
        FieldChange(name, _field(before, name), _field(after, name))
        for name in field_names
        if not _same(_field(before, name), _field(after, name))
    ]
    if asana:
        texts = _diff_keyed_texts(
            _entries(_field(before, "comments"), comment=True),
            _entries(_field(after, "comments"), comment=True),
            added="comment added",
            amended="comment amended",
            removed="comment removed",
        )
    else:
        texts = _diff_keyed_texts(
            _entries(_field(before, "annotations"), comment=False),
            _entries(_field(after, "annotations"), comment=False),
            added="annotation added",
            amended="annotation amended",
            removed="annotation removed",
        )
    return fields, texts


def created_item_changes(
    item: object,
    *,
    asana: bool,
) -> tuple[list[FieldChange], list[TextChange]]:
    """Describe the stored contents of a newly created task."""
    empty_before = {}
    fields, texts = diff_stored_items(empty_before, item, asana=asana)
    fields = [
        field
        for field in fields
        if not _empty(field.after) and not _is_empty_html(field.name, field.after)
    ]
    return fields, texts


def asana_task_url(asana_gid: str | None) -> str | None:
    """Return a browser link that opens this task in Asana."""
    gid = str(asana_gid or "").strip()
    if not gid or gid == "(unknown)":
        return None
    return f"https://app.asana.com/0/0/{gid}"


def task_name_from_item(item: object) -> str:
    """Return the best human-readable name available for a task."""
    if item is None:
        return ""
    client = str(_field(item, "client") or "").strip()
    description = _field(item, "description")
    name = _field(item, "name")
    title = str(description or name or "").strip()
    if client and title and not title.startswith("["):
        return f"[{client}] {title}"
    return title


def _format_record(
    *,
    run_id: str,
    operation: str,
    result: str,
    tw_uuid: str | None,
    asana_gid: str | None,
    task_name: str | None,
    fields: Sequence[FieldChange],
    texts: Sequence[TextChange],
    note: str | None,
    error: str | None,
) -> str:
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "",
        f"[{timestamp}] run={run_id}  {operation}  {result}",
        f"Task: {task_name or '(unknown)'}",
        f"Taskwarrior uuid: {tw_uuid or '(unknown)'}",
        f"Asana gid: {asana_gid or '(unknown)'}",
    ]
    task_url = asana_task_url(asana_gid)
    if task_url is not None:
        lines.append(f"Asana: {task_url}")
    if note:
        lines.append(f"Note: {note}")
    if error:
        lines.append(f"Error: {error}")
    if fields:
        lines.append("Fields:")
        lines.extend(_format_field(operation, field) for field in fields)
    if texts:
        lines.extend(_format_text(change) for change in texts)
    return "\n".join(lines)


def _format_footer(counts: Counter[str], finished_at: datetime.datetime, failed: bool) -> str:
    status = "failed" if failed else "completed"
    total = sum(counts.values())
    lines = [
        "",
        "-" * 80,
        f"Run finished {finished_at.isoformat(timespec='seconds')}  status={status}",
        f"Change records: {total}",
    ]
    for name, count in sorted(counts.items()):
        lines.append(f"  {name}: {count}")
    if total == 0:
        lines.append("No task changes were written during this run.")
    lines.append("-" * 80)
    return "\n".join(lines)


def _format_field(operation: str, field: FieldChange) -> str:
    before = _display(field.name, field.before)
    after = _display(field.name, field.after)
    if operation == "created":
        return f"  {field.name}: {after}"
    if operation == "deleted":
        return f"  {field.name} removed: {before}"
    return f"  {field.name}: {before} -> {after}"


def _format_text(change: TextChange) -> str:
    identity = f" [{change.identity}]" if change.identity else ""
    if change.before and change.after:
        return f"  {change.kind}{identity}: {change.before!r} -> {change.after!r}"
    if change.after:
        return f"  {change.kind}{identity}: {change.after!r}"
    return f"  {change.kind}{identity}: {change.before!r}"


def _display(name: str, value: object) -> str:
    if name == "asana_comment_state":
        return _comment_state_summary(value)
    if _empty(value):
        rendered = "(empty)"
    elif isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            rendered = value.isoformat(timespec="seconds")
        else:
            rendered = value.astimezone().isoformat(timespec="seconds")
    elif isinstance(value, datetime.date):
        rendered = value.isoformat()
    elif isinstance(value, (list, tuple, set)):
        rendered = ", ".join(str(item) for item in value) or "(empty)"
    else:
        text = str(value)
        if "\n" in text:
            rendered = "\n" + "\n".join(f"    {line}" for line in text.splitlines())
        else:
            rendered = text
    return rendered


def _comment_state_summary(value: object) -> str:
    if _empty(value):
        return "(empty)"
    try:
        parsed = json.loads(str(value))
        bindings = parsed.get("bindings", {})
        count = len(bindings) if isinstance(bindings, dict) else 0
    except (TypeError, ValueError):
        return "(comment identity present)"
    return f"{count} comment binding(s)"


def _diff_keyed_texts(
    before: list[tuple[str, str]],
    after: list[tuple[str, str]],
    *,
    added: str,
    amended: str,
    removed: str,
) -> list[TextChange]:
    remaining_after = list(after)
    changes: list[TextChange] = []
    unmatched_before: list[tuple[str, str]] = []
    for key, old in before:
        match_index = None
        if key:
            match_index = next(
                (
                    index
                    for index, (candidate_key, _candidate_text) in enumerate(remaining_after)
                    if candidate_key == key
                ),
                None,
            )
        if match_index is None:
            unmatched_before.append((key, old))
            continue
        new = remaining_after.pop(match_index)[1]
        if old != new:
            changes.append(TextChange(amended, before=old, after=new, identity=key))

    plain_after = [text for key, text in remaining_after if not key]
    changes.extend(
        TextChange(added, after=text, identity=key) for key, text in remaining_after if key
    )
    for key, old in unmatched_before:
        if not key and old in plain_after:
            plain_after.remove(old)
            continue
        changes.append(TextChange(removed, before=old, identity=key or None))
    changes.extend(TextChange(added, after=text) for text in plain_after)
    return changes


def _entries(value: object, *, comment: bool) -> list[tuple[str, str]]:
    if not value:
        return []
    entries: list[tuple[str, str]] = []
    for item in value:  # type: ignore[union-attr]
        if comment:
            entries.append(_comment_entry(item))
        else:
            entries.append(_annotation_entry(item))
    return entries


def _comment_entry(comment: object) -> tuple[str, str]:
    if isinstance(comment, Mapping):
        gid = comment.get("gid") or comment.get("source_id")
        text = str(comment.get("text") or comment.get("description") or "")
    else:
        gid = getattr(comment, "gid", None) or getattr(comment, "source_id", None)
        text = str(getattr(comment, "text", None) or comment)
    return (str(gid) if gid else "", text.strip())


def _annotation_entry(annotation: object) -> tuple[str, str]:
    if isinstance(annotation, Mapping):
        entry = str(annotation.get("entry") or "")
        text = str(annotation.get("description") or "")
        return entry, text.strip()
    return "", str(annotation).strip()


def _field(item: object, name: str) -> object:
    if item is None:
        return None
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _same(before: object, after: object) -> bool:
    return _canonical(before) == _canonical(after)


def _canonical(value: object) -> object:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.UTC)
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return tuple(sorted(str(item) for item in value))
    if _empty(value):
        return None
    return value


def _empty(value: object) -> bool:
    return value is None or value in ("", [], (), {})


def _is_empty_html(name: str, value: object) -> bool:
    return name == "html_notes" and value in (None, "", "<body></body>")


def _append_record(path: Path, text: str) -> None:
    """Append one complete record and flush it to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
