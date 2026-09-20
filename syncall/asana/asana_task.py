from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bubop import parse_datetime

if TYPE_CHECKING:
    from syncall.types import AsanaGID, AsanaRawTask


@dataclass
class AsanaTask(Mapping):
    """Represent an Asana task plus syncable comment text."""

    completed: bool
    completed_at: datetime.datetime | None
    created_at: datetime.datetime
    due_at: datetime.datetime | None
    due_on: datetime.date | None
    name: str
    modified_at: datetime.datetime | None
    html_notes: str = "<body></body>"
    comments: tuple[str, ...] = ()
    gid: AsanaGID | None = None

    _required_key_names: frozenset[str] = frozenset(
        {
            "completed",
            "completed_at",
            "created_at",
            "due_at",
            "due_on",
            "gid",
            "name",
            "modified_at",
        },
    )
    _key_names: frozenset[str] = frozenset(
        {
            *_required_key_names,
            "html_notes",
            "comments",
        },
    )

    def __getitem__(self, key) -> Any:  # noqa: ANN401
        return getattr(self, key)

    def __iter__(self):
        yield from self._key_names

    def __len__(self):
        return len(self._key_names)

    @classmethod
    def from_raw_task(cls, raw_task: AsanaRawTask) -> AsanaTask:
        for key in cls._required_key_names:
            assert key in raw_task

        completed = raw_task["completed"]
        completed_at = None
        if raw_task["completed_at"] is not None:
            completed_at = parse_datetime(raw_task["completed_at"])
        created_at = parse_datetime(raw_task["created_at"])
        due_at = None
        if raw_task["due_at"] is not None:
            due_at = parse_datetime(raw_task["due_at"])
        due_on = None
        if raw_task["due_on"] is not None:
            due_on = datetime.date.fromisoformat(raw_task["due_on"])
        gid = raw_task["gid"]
        modified_at = parse_datetime(raw_task["modified_at"])
        name = raw_task["name"]
        html_notes = raw_task.get("html_notes") or "<body></body>"

        comments_raw = raw_task.get("comments", ())
        comments = tuple(str(comment) for comment in comments_raw)

        return AsanaTask(
            completed=completed,
            completed_at=completed_at,
            created_at=created_at,
            due_at=due_at,
            due_on=due_on,
            gid=gid,
            modified_at=modified_at,
            name=name,
            html_notes=html_notes,
            comments=comments,
        )

    def to_raw_task(self) -> AsanaRawTask:
        raw_task = {
            "completed": self.completed,
            "created_at": self.created_at.isoformat(timespec="milliseconds"),
            "gid": self.gid,
            "name": self.name,
            "html_notes": self.html_notes,
        }

        if self.completed_at is not None:
            raw_task["completed_at"] = self.completed_at.isoformat(timespec="milliseconds")
        else:
            raw_task["completed_at"] = None

        if self.due_at is not None:
            raw_task["due_at"] = self.due_at.isoformat(timespec="milliseconds")
        else:
            raw_task["due_at"] = None

        if self.due_on is not None:
            raw_task["due_on"] = self.due_on.isoformat()
        else:
            raw_task["due_on"] = None

        if self.modified_at is not None:
            raw_task["modified_at"] = self.modified_at.isoformat(timespec="milliseconds")
        else:
            raw_task["modified_at"] = None

        return raw_task
