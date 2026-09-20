from __future__ import annotations

from unittest.mock import MagicMock

from syncall.asana.asana_side import AsanaSide


def _raw_task(gid: str = "1", name: str = "[Client] Test task") -> dict:
    return {
        "completed": False,
        "completed_at": None,
        "created_at": "2026-09-20T12:00:00Z",
        "due_at": None,
        "due_on": None,
        "gid": gid,
        "html_notes": "<body><strong>Short note</strong></body>",
        "modified_at": "2026-09-20T12:30:00Z",
        "name": name,
    }


def _side() -> tuple[AsanaSide, MagicMock]:
    client = MagicMock()
    side = AsanaSide(client=client, task_gid=None, workspace_gid="workspace-1")
    return side, client


def test_task_discovery_combines_assigned_and_follower_only_tasks() -> None:
    side, client = _side()
    client.tasks.find_all.return_value = [{"gid": "1"}, {"gid": "2"}]
    client.tasks.search_in_workspace.return_value = [{"gid": "2"}, {"gid": "3"}]

    tasks = side._get_task_summaries()

    assert [task["gid"] for task in tasks] == ["1", "2", "3"]
    client.tasks.find_all.assert_called_once_with(
        assignee="me",
        workspace="workspace-1",
        page_size=100,
    )
    client.tasks.search_in_workspace.assert_called_once_with(
        "workspace-1",
        params={"followers.any": "me", "assignee.not": "me"},
        page_size=100,
    )


def test_get_item_fetches_rich_notes_and_comment_stories() -> None:
    side, client = _side()
    client.tasks.find_by_id.return_value = _raw_task()
    client.tasks.stories.return_value = [
        {"gid": "s1", "type": "comment", "text": "First comment"},
        {"gid": "s2", "resource_subtype": "comment_added", "text": "Second comment"},
        {"gid": "s3", "type": "system", "text": "changed the due date"},
    ]

    task = side.get_item("1")

    assert task is not None
    assert task.html_notes == "<body><strong>Short note</strong></body>"
    assert task.comments == ("First comment", "Second comment")


def test_update_adds_only_missing_comments() -> None:
    side, client = _side()
    remote = _raw_task()
    client.tasks.find_by_id.return_value = remote
    client.tasks.stories.return_value = [
        {"gid": "s1", "type": "comment", "text": "Existing comment"},
    ]

    task = side.get_item("1")
    assert task is not None

    changes = dict(task)
    changes["comments"] = ("Existing comment", "New comment")
    side.update_item("1", **changes)

    client.tasks.update_task.assert_called_once()
    client.tasks.add_comment.assert_called_once_with("1", text="New comment")


def test_comment_removal_is_not_destructive_on_asana() -> None:
    side, client = _side()
    client.tasks.find_by_id.return_value = _raw_task()
    client.tasks.stories.return_value = [
        {"gid": "s1", "type": "comment", "text": "Remote comment"},
    ]

    task = side.get_item("1")
    assert task is not None

    changes = dict(task)
    changes["comments"] = ()
    side.update_item("1", **changes)

    client.tasks.add_comment.assert_not_called()
