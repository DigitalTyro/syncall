from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import MagicMock

from syncall.asana.asana_task import AsanaComment
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
    side._items_cache = {str(raw_task.get("uuid") or ""): raw_task}
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
            source_entry=datetime.datetime(2024, 2, 3, 10, 15, tzinfo=datetime.UTC),
        ),
        SyncAnnotation(
            "Second comment",
            source_id="story-2",
            source_entry=datetime.datetime(2025, 6, 7, 8, 30, tzinfo=datetime.UTC),
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
            source_entry=datetime.datetime(2025, 1, 2, 12, 0, tzinfo=datetime.UTC),
        ),
        SyncAnnotation(
            "Same text",
            source_id="earlier",
            source_entry=datetime.datetime(2024, 1, 2, 12, 0, tzinfo=datetime.UTC),
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
            source_entry=datetime.datetime(2024, 1, 2, 12, 0, tzinfo=datetime.UTC),
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
                tzinfo=datetime.UTC,
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


def test_record_asana_gid_persists_identity_immediately() -> None:
    side = TaskWarriorSide.__new__(TaskWarriorSide)
    tw = MagicMock()
    side._tw = tw
    side._items_cache = {
        "tw-1": {
            "uuid": "tw-1",
            "description": "Task",
            "status": "pending",
        },
    }
    side._reload_items = False

    recorded = side.record_asana_gid("tw-1", "asana-1")

    assert recorded is True
    tw._execute.assert_called_once_with(
        "tw-1",
        "modify",
        "asana_gid:asana-1",
        "asana_pending_comments:1",
    )
    assert side._items_cache["tw-1"]["asana_gid"] == "asana-1"
    assert side._items_cache["tw-1"]["asana_pending_comments"] == "1"
    assert side._reload_items is True


def test_backfill_asana_gids_updates_only_missing_or_wrong_values() -> None:
    side = TaskWarriorSide.__new__(TaskWarriorSide)
    tw = MagicMock()
    tw._get_json.return_value = [
        {
            "id": 1,
            "uuid": "tw-1",
            "description": "One",
            "status": "pending",
            "urgency": 1.0,
        },
        {
            "id": 2,
            "uuid": "tw-2",
            "description": "Two",
            "status": "pending",
            "urgency": 2.0,
            "asana_gid": "asana-2",
        },
    ]
    imported: list[dict] = []

    def capture_import(command: str, path: str) -> tuple[str, str]:
        assert command == "import"
        with Path(path).open(encoding="utf-8") as handle:
            imported.extend(
                json.loads(line) for line in handle.read().splitlines() if line.strip()
            )
        return "", ""

    tw._execute.side_effect = capture_import
    side._tw = tw
    side._tags = {"asana"}
    side._tw_filter = ""
    side._project = ""
    side._reload_items = False

    changed = side.backfill_asana_gids(
        {"tw-1": "asana-1", "tw-2": "asana-2"},
    )

    assert changed == 1
    assert imported == [
        {
            "uuid": "tw-1",
            "description": "One",
            "status": "pending",
            "asana_gid": "asana-1",
        },
    ]
    assert side._reload_items is True


def test_backfill_asana_gids_is_noop_when_identity_is_already_present() -> None:
    side = TaskWarriorSide.__new__(TaskWarriorSide)
    tw = MagicMock()
    tw._get_json.return_value = [
        {
            "uuid": "tw-1",
            "description": "One",
            "status": "pending",
            "asana_gid": "asana-1",
        },
    ]
    side._tw = tw
    side._tags = {"asana"}
    side._tw_filter = ""
    side._project = ""
    side._reload_items = False

    changed = side.backfill_asana_gids({"tw-1": "asana-1"})

    assert changed == 0
    tw._execute.assert_not_called()


def test_clear_pending_asana_comments_removes_recovery_marker() -> None:
    side = TaskWarriorSide.__new__(TaskWarriorSide)
    tw = MagicMock()
    side._tw = tw
    side._items_cache = {
        "tw-1": {
            "uuid": "tw-1",
            "description": "Task",
            "status": "pending",
            "asana_gid": "asana-1",
            "asana_pending_comments": "1",
        },
    }
    side._reload_items = False

    side.clear_pending_asana_comments("tw-1")

    tw._execute.assert_called_once_with(
        "tw-1",
        "modify",
        "asana_pending_comments:",
    )
    assert "asana_pending_comments" not in side._items_cache["tw-1"]
    assert side._reload_items is True


def test_reconcile_comment_state_rebuilds_from_remote_gid_and_timestamp() -> None:
    raw_task = {
        "uuid": "tw-1",
        "description": "Task",
        "status": "pending",
        "annotations": [
            {
                "entry": "20240105T091500Z",
                "description": "Imported Asana comment",
            },
        ],
    }
    side, _, imported = _side_with_export(raw_task)
    remote = [
        AsanaComment(
            "Imported Asana comment",
            gid="story-1",
            created_at=datetime.datetime(2024, 1, 5, 9, 15, tzinfo=datetime.UTC),
        ),
    ]

    unsynced = side.reconcile_asana_comment_state("tw-1", raw_task, remote)

    assert unsynced == []
    assert len(imported) == 1
    state = json.loads(imported[0]["asana_comment_state"])
    assert state["version"] == 1
    assert state["bindings"]["story-1"]["e"] == "20240105T091500Z"


def test_reconcile_comment_state_backfills_missed_local_annotation() -> None:
    raw_task = {
        "uuid": "tw-1",
        "description": "Task",
        "status": "pending",
        "annotations": [
            {
                "entry": "20240105T091500Z",
                "description": "Already remote",
            },
            {
                "entry": "20260923T170000Z",
                "description": "Missed local comment",
            },
        ],
    }
    side, _, _ = _side_with_export(raw_task)
    remote = [
        AsanaComment(
            "Already remote",
            gid="story-1",
            created_at=datetime.datetime(2024, 1, 5, 9, 15, tzinfo=datetime.UTC),
        ),
    ]

    unsynced = side.reconcile_asana_comment_state("tw-1", raw_task, remote)

    assert unsynced == ["Missed local comment"]


def test_reconcile_comment_state_recovers_when_syncall_caches_are_gone() -> None:
    raw_task = {
        "uuid": "tw-1",
        "description": "Task",
        "status": "pending",
        "annotations": [
            {
                "entry": "20260921T120100Z",
                "description": "Imported Asana comment",
            },
        ],
    }
    side, _, imported = _side_with_export(raw_task)
    remote = [
        AsanaComment(
            "Imported Asana comment",
            gid="story-1",
            created_at=datetime.datetime(2024, 1, 5, 9, 15, tzinfo=datetime.UTC),
        ),
    ]
    # No asana_comment_state exists and the annotation has the old pre-repair timestamp.
    unsynced = side.reconcile_asana_comment_state("tw-1", raw_task, remote)

    assert unsynced == []
    state = json.loads(imported[0]["asana_comment_state"])
    assert "story-1" in state["bindings"]


def test_reconcile_comment_state_does_not_repost_edited_bound_annotation() -> None:
    original = {
        "uuid": "tw-1",
        "description": "Task",
        "status": "pending",
        "annotations": [
            {
                "entry": "20240105T091500Z",
                "description": "Original remote text",
            },
        ],
    }
    side, _, imported = _side_with_export(original)
    remote = [
        AsanaComment(
            "Original remote text",
            gid="story-1",
            created_at=datetime.datetime(2024, 1, 5, 9, 15, tzinfo=datetime.UTC),
        ),
    ]
    assert side.reconcile_asana_comment_state("tw-1", original, remote) == []
    state = imported[-1]["asana_comment_state"]

    edited = {
        **original,
        "asana_comment_state": state,
        "annotations": [
            {
                "entry": "20240105T091500Z",
                "description": "Edited locally but same annotation",
            },
        ],
    }
    side._tw._get_json.return_value = [edited]
    side._items_cache["tw-1"] = edited
    imported.clear()

    assert side.reconcile_asana_comment_state("tw-1", edited, remote) == []
    assert imported


def test_deleted_remote_comment_tombstone_prevents_resurrection() -> None:
    raw_task = {
        "uuid": "tw-1",
        "description": "Task",
        "status": "pending",
        "asana_comment_state": json.dumps(
            {
                "version": 1,
                "bindings": {
                    "story-deleted": {
                        "e": "20240105T091500Z",
                        "h": TaskWarriorSide._annotation_text_digest("Deleted remotely"),
                    },
                },
            },
        ),
        "annotations": [
            {
                "entry": "20240105T091500Z",
                "description": "Deleted remotely",
            },
        ],
    }
    side, _, _ = _side_with_export(raw_task)

    assert side.reconcile_asana_comment_state("tw-1", raw_task, []) == []


def test_duplicate_text_prefers_remote_created_time_and_keeps_new_local_duplicate() -> None:
    raw_task = {
        "uuid": "tw-1",
        "description": "Task",
        "status": "pending",
        "annotations": [
            {
                "entry": "20240105T091600Z",
                "description": "Same text",
            },
            {
                "entry": "20260923T170000Z",
                "description": "Same text",
            },
        ],
    }
    side, _, _ = _side_with_export(raw_task)
    remote = [
        AsanaComment(
            "Same text",
            gid="story-1",
            created_at=datetime.datetime(2024, 1, 5, 9, 15, tzinfo=datetime.UTC),
        ),
    ]

    assert side.reconcile_asana_comment_state("tw-1", raw_task, remote) == ["Same text"]


def test_reconcile_comment_state_imports_missing_remote_annotation_directly() -> None:
    raw_task = {
        "uuid": "tw-1",
        "description": "Task",
        "status": "pending",
        "annotations": [],
    }
    side, _, imported = _side_with_export(raw_task)
    remote = [
        AsanaComment(
            "New remote comment",
            gid="story-new",
            created_at=datetime.datetime(2026, 9, 23, 17, 0, tzinfo=datetime.UTC),
        ),
    ]

    assert side.reconcile_asana_comment_state("tw-1", raw_task, remote) == []
    assert imported[-1]["annotations"] == [
        {
            "entry": "20260923T170000Z",
            "description": "New remote comment",
        },
    ]


def test_bound_comment_does_not_capture_new_same_text_annotation() -> None:
    raw_task = {
        "uuid": "tw-1",
        "description": "Task",
        "status": "pending",
        "asana_comment_state": json.dumps(
            {
                "version": 1,
                "bindings": {
                    "story-1": {
                        "e": "20240105T091500Z",
                        "h": TaskWarriorSide._annotation_text_digest("Same text"),
                    },
                },
            },
        ),
        "annotations": [
            {
                "entry": "20260923T170000Z",
                "description": "Same text",
            },
        ],
    }
    side, _, _ = _side_with_export(raw_task)
    remote = [
        AsanaComment(
            "Same text",
            gid="story-1",
            created_at=datetime.datetime(2024, 1, 5, 9, 15, tzinfo=datetime.UTC),
        ),
    ]

    assert side.reconcile_asana_comment_state("tw-1", raw_task, remote) == ["Same text"]
