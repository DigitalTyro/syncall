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
        params={
            "followers.any": "me",
            "assignee.not": "me",
            "sort_by": "created_at",
            "sort_ascending": True,
        },
        fields=["gid", "created_at"],
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


def test_unchanged_markdown_preserves_asana_only_rich_metadata() -> None:
    side, client = _side()
    remote = _raw_task()
    remote["html_notes"] = (
        '<body><a href="https://app.asana.com/0/0/123" '
        'data-asana-type="user" data-asana-gid="123">@Sam</a></body>'
    )
    client.tasks.find_by_id.return_value = remote
    client.tasks.stories.return_value = []

    task = side.get_item("1")
    assert task is not None

    changes = dict(task)
    changes["html_notes"] = '<body><a href="https://app.asana.com/0/0/123">@Sam</a></body>'
    side.update_item("1", **changes)

    _, raw_update = client.tasks.update_task.call_args.args
    assert "html_notes" not in raw_update


def test_follower_discovery_pages_by_created_at() -> None:
    side, client = _side()
    first_page = [
        {"gid": str(index), "created_at": f"2026-01-01T00:00:{index % 60:02d}.000Z"}
        for index in range(100)
    ]
    first_page[-1]["created_at"] = "2026-01-02T00:00:00.000Z"
    second_page = [{"gid": "101", "created_at": "2026-01-03T00:00:00.000Z"}]
    client.tasks.search_in_workspace.side_effect = [first_page, second_page]

    tasks = side._get_follower_task_summaries()

    assert len(tasks) == 101
    second_call = client.tasks.search_in_workspace.call_args_list[1]
    assert second_call.kwargs["params"]["created_at.after"] == "2026-01-02T00:00:00.000Z"
