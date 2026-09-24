from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest
from bidict import bidict
from syncall.aggregator import Aggregator
from syncall.asana.asana_side import AsanaSide
from syncall.asana.asana_task import AsanaComment, AsanaTask
from syncall.change_log import (
    TO_ASANA,
    TO_TASKWARRIOR,
    SyncChangeLog,
    diff_stored_items,
    task_name_from_item,
)
from syncall.taskwarrior.taskwarrior_side import TaskWarriorSide


def _asana_task(name: str, **overrides: object) -> dict:
    task = {
        "gid": "asana-1",
        "name": name,
        "completed": False,
        "completed_at": None,
        "due_on": None,
        "due_at": None,
        "html_notes": "<body></body>",
        "comments": (),
    }
    task.update(overrides)
    return task


def test_task_name_from_asana_task_ignores_missing_client() -> None:
    task = AsanaTask(
        completed=False,
        completed_at=None,
        created_at=datetime.datetime(2026, 9, 24, tzinfo=datetime.UTC),
        due_at=None,
        due_on=None,
        name="[Acme] Proposal",
        modified_at=None,
        gid="asana-1",
    )

    assert task_name_from_item(task) == "[Acme] Proposal"


def test_each_run_appends_a_header_to_both_logs(tmp_path) -> None:
    first = SyncChangeLog(tmp_path)
    first.finish(failed=False)
    second = SyncChangeLog(tmp_path)
    second.record(
        direction=TO_ASANA,
        operation="updated",
        result="succeeded",
        tw_uuid="tw-1",
        asana_gid="asana-1",
        task_name="Write proposal",
        fields=(),
    )
    second.finish(failed=False)

    asana_log = (tmp_path / "taskwarrior-to-asana.log").read_text()
    taskwarrior_log = (tmp_path / "asana-to-taskwarrior.log").read_text()
    assert asana_log.count("Run ") >= 4
    assert first.run_id in asana_log
    assert second.run_id in asana_log
    assert "No task changes were written during this run." in taskwarrior_log
    assert "updated  succeeded" not in asana_log


def test_field_comment_and_annotation_changes_include_before_and_after() -> None:
    fields, texts = diff_stored_items(
        {
            "name": "[Acme] Old",
            "html_notes": "<body>Keep the image</body>",
            "completed": False,
            "comments": (AsanaComment("Original", gid="story-1"),),
        },
        {
            "name": "[Acme] New",
            "html_notes": "<body>Image removed</body>",
            "completed": False,
            "comments": (
                AsanaComment("Rewritten", gid="story-1"),
                AsanaComment("Added later", gid="story-2"),
            ),
        },
        asana=True,
    )

    rendered = {field.name: (field.before, field.after) for field in fields}
    assert rendered["name"] == ("[Acme] Old", "[Acme] New")
    assert rendered["html_notes"] == (
        "<body>Keep the image</body>",
        "<body>Image removed</body>",
    )
    assert "completed" not in rendered
    assert [change.kind for change in texts] == ["comment amended", "comment added"]
    assert texts[0].before == "Original"
    assert texts[0].after == "Rewritten"
    assert texts[1].after == "Added later"


def test_signed_asset_url_refresh_is_not_logged_as_html_notes_change() -> None:
    fields, texts = diff_stored_items(
        {
            "name": "Task",
            "html_notes": (
                '<body><img src="https://asanausercontent.com/us1/assets/1/2/abc'
                '?e=1790276561&amp;v=0&amp;t=OldToken" /></body>'
            ),
        },
        {
            "name": "Task",
            "html_notes": (
                '<body><img src="https://asanausercontent.com/us1/assets/1/2/abc'
                '?e=1790276562&amp;v=0&amp;t=NewToken" /></body>'
            ),
        },
        asana=True,
    )

    assert fields == []
    assert texts == []

    _fields, annotation_changes = diff_stored_items(
        {
            "annotations": ({"entry": "20260921T120000Z", "description": "Local note"},),
        },
        {
            "annotations": (
                {"entry": "20260921T120000Z", "description": "Local note edited"},
            ),
        },
        asana=False,
    )
    assert annotation_changes[0].kind == "annotation amended"
    assert annotation_changes[0].before == "Local note"
    assert annotation_changes[0].after == "Local note edited"


def test_log_append_failure_does_not_raise(tmp_path) -> None:
    change_log = SyncChangeLog(tmp_path)
    with patch("syncall.change_log._append_record", side_effect=OSError("disk full")):
        change_log.record(
            direction=TO_TASKWARRIOR,
            operation="updated",
            result="succeeded",
            task_name="Still logged in memory only",
            fields=(),
        )
    change_log.finish(failed=True)


def test_new_asana_comment_is_logged_and_existing_comment_is_not(tmp_path) -> None:
    change_log = SyncChangeLog(tmp_path)
    client = MagicMock()
    side = AsanaSide(client=client, task_gid=None, workspace_gid="workspace-1")
    side.change_log = change_log
    client.tasks.stories.return_value = [
        {"gid": "story-1", "type": "comment", "text": "Already there"},
    ]

    with change_log.task_context(
        tw_uuid="tw-1",
        asana_gid="asana-1",
        task_name="[Acme] Proposal",
    ):
        side._add_missing_comments("asana-1", ["Already there", "Fresh comment"])

    client.tasks.add_comment.assert_called_once_with("asana-1", text="Fresh comment")
    log = change_log.path_for(TO_ASANA).read_text()
    assert "Fresh comment" in log
    assert "https://app.asana.com/0/0/asana-1" in log
    assert "Already there" not in log
    assert "[Acme] Proposal" in log
    assert "tw-1" in log
    change_log.finish(failed=False)


def test_updater_logs_only_the_stored_field_change(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.name = "Asana"
    helper.id_key = "gid"
    helper.summary_key = "name"
    helper.other = "Tw"
    aggregator._helper_A = helper
    aggregator._helper_B = MagicMock()
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator.change_log = SyncChangeLog(tmp_path)
    aggregator._operation_failed = False
    aggregator._written_serdes = set()
    aggregator._advance_operation_progress = MagicMock()
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    before = _asana_task("Old name")
    after = _asana_task("New name")
    side = MagicMock()
    side.get_item.side_effect = [before, after]
    aggregator._get_side_instances = MagicMock(return_value=(side, MagicMock()))

    with patch("syncall.aggregator.pickle_dump"):
        aggregator.updater_to("asana-1", after, helper)

    asana_log = aggregator.change_log.path_for(TO_ASANA).read_text()
    taskwarrior_log = aggregator.change_log.path_for(TO_TASKWARRIOR).read_text()
    assert "Old name -> New name" in asana_log
    assert "https://app.asana.com/0/0/asana-1" in asana_log
    assert "updated  succeeded" in asana_log
    assert "Old name" not in taskwarrior_log


def test_unchanged_update_is_not_recorded(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.name = "Asana"
    helper.summary_key = "name"
    aggregator._helper_A = helper
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator.change_log = SyncChangeLog(tmp_path)
    aggregator._operation_failed = False
    aggregator._written_serdes = set()
    aggregator._advance_operation_progress = MagicMock()
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    stored = _asana_task("Same name")
    side = MagicMock()
    side.get_item.return_value = stored
    aggregator._get_side_instances = MagicMock(return_value=(side, MagicMock()))

    with patch("syncall.aggregator.pickle_dump"):
        aggregator.updater_to("asana-1", stored, helper)

    log = aggregator.change_log.path_for(TO_ASANA).read_text()
    assert "updated  succeeded" not in log


def test_failed_update_is_logged_and_still_raises(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.name = "Tw"
    helper.summary_key = "description"
    aggregator._helper_B = helper
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator.change_log = SyncChangeLog(tmp_path)
    aggregator._operation_failed = False
    aggregator._written_serdes = set()
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    stored = {"uuid": "tw-1", "description": "Proposal", "asana_gid": "asana-1"}
    side = MagicMock()
    side.get_item.return_value = stored
    side.update_item.side_effect = RuntimeError("Asana rejected the write")
    aggregator._get_side_instances = MagicMock(return_value=(side, MagicMock()))

    with (
        patch("syncall.aggregator.pickle_dump") as pickle_dump,
        pytest.raises(RuntimeError, match="rejected"),
    ):
        aggregator.updater_to("tw-1", stored, helper)

    assert aggregator._operation_failed is True
    pickle_dump.assert_called_once()
    log = aggregator.change_log.path_for(TO_TASKWARRIOR).read_text()
    assert "updated  failed" in log
    assert "RuntimeError: Asana rejected the write" in log
    assert "Proposal" in log


def test_taskwarrior_identity_backfill_is_logged_as_bookkeeping(tmp_path) -> None:
    change_log = SyncChangeLog(tmp_path)
    side = TaskWarriorSide.__new__(TaskWarriorSide)
    side._tw = MagicMock()
    side._tw._get_json.return_value = [
        {"uuid": "tw-1", "description": "Proposal", "id": 4, "urgency": 1},
    ]
    side._tags = set()
    side._tw_filter = ""
    side._project = ""
    side._reload_items = False
    side.change_log = change_log

    changed = side.backfill_asana_gids({"tw-1": "asana-9"})

    assert changed == 1
    log = change_log.path_for(TO_TASKWARRIOR).read_text()
    assert "identity stored  succeeded" in log
    assert "asana-9" in log
    assert "https://app.asana.com/0/0/asana-9" in log
    assert "Asana task content was not changed" in log
    assert "identity stored" not in change_log.path_for(TO_ASANA).read_text()
