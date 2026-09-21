from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import MagicMock

from syncall.taskwarrior.taskwarrior_side import TaskWarriorSide
from syncall.types import SyncAnnotation


def _side_with_export(raw_task: dict) -> tuple[TaskWarriorSide, MagicMock, list[dict]]:
    side = TaskWarriorSide.__new__(TaskWarriorSide)
    tw = MagicMock()
    tw._get_json.return_value = [raw_task]
    imported: list[dict] = []

    def capture_import(command: str, path: str) -> tuple[str, str]:
        assert command == "import"
        with Path(path).open(encoding="utf-8") as handle:
            imported.append(json.loads(handle.read()))
        return "", ""

    tw._execute.side_effect = capture_import
    side._tw = tw
    side._reload_items = False
    return side, tw, imported


def test_annotation_timestamp_repair_updates_only_entry_values() -> None:
    raw_task = {
        "id": 42,
        "uuid": "11111111-1111-1111-1111-111111111111",
        "description": "Test task",
        "entry": "20240101T090000Z",
        "modified": "20260921T120000Z",
        "status": "pending",
        "urgency": 1.2,
        "annotations": [
            {
                "entry": "20260921T120100Z",
                "description": "First comment",
            },
            {
                "entry": "20260921T120200Z",
                "description": "Second comment",
            },
        ],
    }
    side, tw, imported = _side_with_export(raw_task)
    desired = [
        SyncAnnotation(
            "First comment",
            source_id="story-1",
            source_entry=datetime.datetime(2024, 2, 3, 10, 15, tzinfo=datetime.timezone.utc),
        ),
        SyncAnnotation(
            "Second comment",
            source_id="story-2",
            source_entry=datetime.datetime(2025, 6, 7, 8, 30, tzinfo=datetime.timezone.utc),
        ),
    ]

    repaired = side._repair_annotation_timestamps(raw_task["uuid"], desired)

    assert repaired == 2
    assert len(imported) == 1
    imported_task = imported[0]
    assert "id" not in imported_task
    assert "urgency" not in imported_task
    assert imported_task["uuid"] == raw_task["uuid"]
    assert imported_task["description"] == raw_task["description"]
    assert imported_task["modified"] == raw_task["modified"]
    assert imported_task["annotations"] == [
        {
            "entry": "20240203T101500Z",
            "description": "First comment",
        },
        {
            "entry": "20250607T083000Z",
            "description": "Second comment",
        },
    ]
    assert side._reload_items is True
    tw._execute.assert_called_once()


def test_duplicate_annotation_text_is_paired_chronologically() -> None:
    raw_task = {
        "uuid": "22222222-2222-2222-2222-222222222222",
        "description": "Test duplicates",
        "entry": "20240101T090000Z",
        "modified": "20260921T120000Z",
        "status": "pending",
        "annotations": [
            {"entry": "20260921T120100Z", "description": "Same text"},
            {"entry": "20260921T120200Z", "description": "Same text"},
        ],
    }
    side, _, imported = _side_with_export(raw_task)
    desired = [
        SyncAnnotation(
            "Same text",
            source_id="later",
            source_entry=datetime.datetime(2025, 1, 2, 12, 0, tzinfo=datetime.timezone.utc),
        ),
        SyncAnnotation(
            "Same text",
            source_id="earlier",
            source_entry=datetime.datetime(2024, 1, 2, 12, 0, tzinfo=datetime.timezone.utc),
        ),
    ]

    repaired = side._repair_annotation_timestamps(raw_task["uuid"], desired)

    assert repaired == 2
    assert [annotation["entry"] for annotation in imported[0]["annotations"]] == [
        "20240102T120000Z",
        "20250102T120000Z",
    ]


def test_ambiguous_annotation_count_is_not_guessed() -> None:
    raw_task = {
        "uuid": "33333333-3333-3333-3333-333333333333",
        "description": "Test ambiguity",
        "entry": "20240101T090000Z",
        "modified": "20260921T120000Z",
        "status": "pending",
        "annotations": [
            {"entry": "20260921T120100Z", "description": "Same text"},
            {"entry": "20260921T120200Z", "description": "Same text"},
        ],
    }
    side, tw, imported = _side_with_export(raw_task)
    desired = [
        SyncAnnotation(
            "Same text",
            source_id="only-one",
            source_entry=datetime.datetime(2024, 1, 2, 12, 0, tzinfo=datetime.timezone.utc),
        ),
    ]

    repaired = side._repair_annotation_timestamps(raw_task["uuid"], desired)

    assert repaired == 0
    assert imported == []
    tw._execute.assert_not_called()


def test_reconcile_skips_live_export_when_annotation_dates_are_already_correct() -> None:
    side = TaskWarriorSide.__new__(TaskWarriorSide)
    tw = MagicMock()
    side._tw = tw
    side._reload_items = False
    desired = [
        SyncAnnotation(
            "Historical comment",
            source_id="story-1",
            source_entry=datetime.datetime(
                2024,
                3,
                4,
                12,
                34,
                56,
                tzinfo=datetime.timezone.utc,
            ),
        ),
    ]
    current = [
        {
            "entry": "20240304T123456Z",
            "description": "Historical comment",
        },
    ]

    repaired = side.reconcile_annotation_timestamps(
        "44444444-4444-4444-4444-444444444444",
        desired,
        current_annotations=current,
    )

    assert repaired == 0
    tw._get_json.assert_not_called()
    tw._execute.assert_not_called()
