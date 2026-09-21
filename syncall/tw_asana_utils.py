"""Asana-related conversion utilities."""

import datetime
import re

import dateutil
from bubop import logger, parse_datetime

from syncall.asana.asana_task import AsanaComment, AsanaTask
from syncall.asana.rich_text import asana_html_to_markdown, markdown_to_asana_html
from syncall.types import SyncAnnotation, TwItem

_CLIENT_PREFIX_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")


def split_client_prefix(name: str) -> tuple[str | None, str]:
    """Split an Asana task name such as [Client] Task into client and task name."""
    match = _CLIENT_PREFIX_RE.match(name)
    if match is None:
        return None, name

    client = match.group(1).strip()
    description = match.group(2).strip()
    if not client:
        return None, name
    return client, description


def build_asana_name(description: str, client: str | None) -> str:
    """Reconstruct an Asana task name from Taskwarrior fields."""
    client = (client or "").strip()
    description = description.strip()
    if not client:
        return description
    return f"[{client}] {description}"


def convert_tw_to_asana(tw_item: TwItem) -> AsanaTask:
    tw_description = tw_item["description"]
    tw_due = tw_item.get("due")
    tw_end = tw_item.get("end")
    tw_entry = tw_item["entry"]
    tw_modified = tw_item["modified"]
    tw_status = tw_item["status"]

    as_completed = False
    as_completed_at = None
    as_created_at = None
    as_due_at = None
    as_due_on = None
    as_modified_at = None

    if tw_status == "completed":
        as_completed = True
        if tw_end is not None:
            if isinstance(tw_end, datetime.datetime):
                as_completed_at = tw_end
            else:
                as_completed_at = parse_datetime(tw_end)

    if not isinstance(tw_entry, datetime.datetime):
        as_created_at = parse_datetime(tw_entry)
    else:
        as_created_at = tw_entry

    if tw_due is not None:
        as_due_at = tw_due if isinstance(tw_due, datetime.datetime) else parse_datetime(tw_due)
        as_due_on = as_due_at.date()

    if isinstance(tw_modified, datetime.datetime):
        as_modified_at = tw_modified
    else:
        as_modified_at = parse_datetime(tw_modified)

    as_name = build_asana_name(tw_description, tw_item.get("client"))
    as_html_notes = markdown_to_asana_html(str(tw_item.get("notes") or ""))
    as_comments = tuple(
        AsanaComment(text=str(annotation)) for annotation in tw_item.get("annotations", ())
    )

    return AsanaTask(
        completed=as_completed,
        completed_at=as_completed_at,
        created_at=as_created_at,
        due_at=as_due_at,
        due_on=as_due_on,
        modified_at=as_modified_at,
        name=as_name,
        html_notes=as_html_notes,
        comments=as_comments,
    )


def convert_asana_to_tw(asana_task: AsanaTask) -> TwItem | None:  # noqa: C901, PLR0912
    as_completed = asana_task["completed"]
    as_completed_at = asana_task["completed_at"]
    as_created_at = asana_task["created_at"]
    as_due_at = asana_task["due_at"]
    as_due_on = asana_task["due_on"]
    as_modified_at = asana_task["modified_at"]
    as_name = asana_task["name"]

    tw_due = None
    tw_end = None
    tw_entry = None
    tw_modified = None
    tw_status = "pending"

    if isinstance(as_created_at, datetime.datetime):
        tw_entry = as_created_at
    else:
        tw_entry = parse_datetime(as_created_at)

    if as_completed:
        if isinstance(as_completed_at, datetime.datetime):
            tw_end = as_completed_at
        else:
            tw_end = parse_datetime(as_completed_at)
        tw_status = "completed"

    if as_modified_at is not None:
        if isinstance(as_modified_at, datetime.datetime):
            tw_modified = as_modified_at
        else:
            tw_modified = parse_datetime(as_modified_at)

    if as_due_at is not None:
        if isinstance(as_due_at, datetime.datetime):
            tw_due = as_due_at
        else:
            tw_due = parse_datetime(as_due_at)
    elif as_due_on is not None:
        if isinstance(as_due_on, datetime.date):
            tw_due = datetime.datetime.combine(
                as_due_on,
                datetime.time(0, 0, 0),
                dateutil.tz.tzlocal(),
            )
        else:
            tw_due = parse_datetime(as_due_on)

    client, tw_description = split_client_prefix(as_name)
    if not tw_description.strip():
        logger.warning(
            f"Skipping Asana task {asana_task.get('gid') or '<unknown>'}: "
            "Taskwarrior description would be blank.",
        )
        return None

    tw_task = {
        "description": tw_description,
        "due": None,
        "end": tw_end,
        "entry": tw_entry,
        "modified": tw_modified,
        "status": tw_status,
        "notes": asana_html_to_markdown(str(asana_task.get("html_notes") or "")),
        "annotations": [
            SyncAnnotation(
                comment.text,
                source_id=str(comment.gid) if comment.gid is not None else None,
                source_entry=comment.created_at,
            )
            for comment in (
                AsanaComment.from_raw(raw_comment)
                for raw_comment in asana_task.get("comments", ())
            )
        ],
    }

    if client is not None:
        tw_task["client"] = client

    if tw_due is not None:
        tw_task["due"] = tw_due

    if tw_modified is not None:
        tw_task["modified"] = tw_modified

    return tw_task
