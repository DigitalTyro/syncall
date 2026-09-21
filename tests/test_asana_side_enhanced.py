from __future__ import annotations

from unittest.mock import MagicMock

from syncall.asana.asana_side import AsanaSide
from syncall.asana.asana_task import AsanaComment, AsanaTask


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
        fields=[
            "completed",
            "completed_at",
            "created_at",
            "due_at",
            "due_on",
            "gid",
            "html_notes",
            "modified_at",
            "name",
        ],
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
        fields=[
            "completed",
            "completed_at",
            "created_at",
            "due_at",
            "due_on",
            "gid",
            "html_notes",
            "modified_at",
            "name",
        ],
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
    assert [str(comment) for comment in task.comments] == ["First comment", "Second comment"]


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


def test_get_all_items_reuses_detailed_discovery_payloads(tmp_path) -> None:
    client = MagicMock()
    side = AsanaSide(
        client=client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=tmp_path / "comments.json",
    )
    client.tasks.find_all.return_value = [_raw_task("1")]
    client.tasks.search_in_workspace.return_value = []
    client.tasks.stories.return_value = []

    tasks = side.get_all_items()

    assert [task.gid for task in tasks] == ["1"]
    client.tasks.find_by_id.assert_not_called()
    client.tasks.stories.assert_called_once_with(
        "1",
        fields=["created_at", "gid", "resource_subtype", "text", "type"],
        page_size=100,
    )


def test_comment_cache_skips_unchanged_story_fetch(tmp_path) -> None:
    cache_path = tmp_path / "comments.json"
    client = MagicMock()
    side = AsanaSide(
        client=client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=cache_path,
    )
    raw_task = _raw_task("1")
    client.tasks.find_all.return_value = [raw_task]
    client.tasks.search_in_workspace.return_value = []
    client.tasks.stories.return_value = [
        {
            "created_at": "2025-04-02T09:15:00Z",
            "gid": "s1",
            "type": "comment",
            "text": "Cached comment",
        },
    ]

    first = side.get_all_items()
    side.finish()

    second_client = MagicMock()
    second_side = AsanaSide(
        client=second_client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=cache_path,
    )
    second_client.tasks.find_all.return_value = [raw_task]
    second_client.tasks.search_in_workspace.return_value = []

    second = second_side.get_all_items()

    assert [str(comment) for comment in first[0].comments] == ["Cached comment"]
    assert first[0].comments[0].gid == "s1"
    assert [str(comment) for comment in second[0].comments] == ["Cached comment"]
    assert second[0].comments[0].gid == "s1"
    second_client.tasks.stories.assert_not_called()


def test_legacy_comment_cache_refreshes_nonempty_entries_once(tmp_path) -> None:
    cache_path = tmp_path / "comments.json"
    cache_path.write_text(
        '{"1":{"modified_at":"2026-09-20T12:30:00Z","comments":["Old comment"]}}',
    )
    client = MagicMock()
    side = AsanaSide(
        client=client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=cache_path,
    )
    raw_task = _raw_task("1")
    client.tasks.find_all.return_value = [raw_task]
    client.tasks.search_in_workspace.return_value = []
    client.tasks.stories.return_value = [
        {
            "created_at": "2025-01-02T10:30:00Z",
            "gid": "story-1",
            "type": "comment",
            "text": "Old comment",
        },
    ]

    tasks = side.get_all_items()
    side.finish()

    assert tasks[0].comments[0].gid == "story-1"
    assert tasks[0].comments[0].created_at is not None
    client.tasks.stories.assert_called_once()
    saved = cache_path.read_text()
    assert '"version": 2' in saved
    assert '"created_at": "2025-01-02T10:30:00.000+00:00"' in saved


def test_legacy_empty_comment_cache_upgrades_without_refetch(tmp_path) -> None:
    cache_path = tmp_path / "comments.json"
    cache_path.write_text(
        '{"1":{"modified_at":"2026-09-20T12:30:00Z","comments":[]}}',
    )
    client = MagicMock()
    side = AsanaSide(
        client=client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=cache_path,
    )
    client.tasks.find_all.return_value = [_raw_task("1")]
    client.tasks.search_in_workspace.return_value = []

    tasks = side.get_all_items()
    side.finish()

    assert tasks[0].comments == ()
    client.tasks.stories.assert_not_called()
    assert '"version": 2' in cache_path.read_text()


def test_comment_metadata_does_not_create_false_sync_change() -> None:
    legacy = AsanaTask.from_raw_task({**_raw_task("1"), "comments": ["Same comment"]})
    legacy.comments = ("Same comment",)  # type: ignore[assignment]
    structured = AsanaTask.from_raw_task(
        {
            **_raw_task("1"),
            "comments": [
                AsanaComment.from_raw(
                    {
                        "created_at": "2025-01-02T10:30:00Z",
                        "gid": "story-1",
                        "text": "Same comment",
                    },
                ),
            ],
        },
    )

    assert AsanaSide.items_are_identical(legacy, structured)

    structured.comments = (AsanaComment(text="Changed comment"),)
    assert not AsanaSide.items_are_identical(legacy, structured)


def test_structured_comment_cache_periodically_refreshes_edited_comments(tmp_path) -> None:
    cache_path = tmp_path / "comments.json"
    cache_path.write_text(
        '{"1":{"version":2,"modified_at":"2026-09-20T12:30:00Z",'
        '"checked_at":"2020-01-01T00:00:00+00:00","comments":['
        '{"gid":"story-1","text":"Old text","created_at":"2025-01-02T10:30:00+00:00"}]}}',
    )
    client = MagicMock()
    side = AsanaSide(
        client=client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=cache_path,
    )
    client.tasks.find_all.return_value = [_raw_task("1")]
    client.tasks.search_in_workspace.return_value = []
    client.tasks.stories.return_value = [
        {
            "created_at": "2025-01-02T10:30:00Z",
            "gid": "story-1",
            "type": "comment",
            "text": "Edited text",
        },
    ]

    tasks = side.get_all_items()

    assert [str(comment) for comment in tasks[0].comments] == ["Edited text"]
    client.tasks.stories.assert_called_once()


def test_existing_comment_is_never_readded() -> None:
    side, client = _side()
    client.tasks.stories.return_value = [
        {"gid": "s1", "type": "comment", "text": "Existing comment"},
    ]

    side._add_missing_comments("1", ["Existing comment"])

    client.tasks.add_comment.assert_not_called()


def test_existing_comment_match_ignores_line_endings_and_outer_whitespace() -> None:
    side, client = _side()
    client.tasks.stories.return_value = [
        {
            "gid": "s1",
            "type": "comment",
            "text": "  First line\r\nSecond line   ",
        },
    ]

    side._add_missing_comments("1", ["First line\nSecond line"])

    client.tasks.add_comment.assert_not_called()


def test_duplicate_desired_annotations_create_at_most_one_comment() -> None:
    side, client = _side()
    client.tasks.stories.side_effect = [[], []]

    side._add_missing_comments("1", ["New comment", "New comment"])

    client.tasks.add_comment.assert_called_once_with("1", text="New comment")


def test_live_recheck_prevents_race_duplicate() -> None:
    side, client = _side()
    client.tasks.stories.side_effect = [
        [],
        [{"gid": "s1", "type": "comment", "text": "New comment"}],
    ]

    side._add_missing_comments("1", ["New comment"])

    client.tasks.add_comment.assert_not_called()


def test_comment_write_does_not_depend_on_local_cache(tmp_path) -> None:
    missing_cache = tmp_path / "missing-comments.json"
    side = AsanaSide(
        client=MagicMock(),
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=missing_cache,
    )
    side._client.tasks.stories.return_value = [
        {"gid": "s1", "type": "comment", "text": "Already remote"},
    ]

    side._add_missing_comments("1", ["Already remote"])

    side._client.tasks.add_comment.assert_not_called()


def test_server_success_then_client_error_does_not_duplicate_on_retry() -> None:
    side, client = _side()
    client.tasks.stories.side_effect = [[], []]
    client.tasks.add_comment.side_effect = RuntimeError("connection dropped")

    try:
        side._add_missing_comments("1", ["Maybe created"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected simulated network failure")

    client.tasks.add_comment.reset_mock()
    client.tasks.add_comment.side_effect = None
    client.tasks.stories.side_effect = [
        [{"gid": "s1", "type": "comment", "text": "Maybe created"}],
    ]

    side._add_missing_comments("1", ["Maybe created"])

    client.tasks.add_comment.assert_not_called()


def test_new_task_creation_does_not_add_comments_before_return() -> None:
    side, client = _side()
    client.tasks.create_task.return_value = {"gid": "new-task"}
    client.tasks.find_by_id.return_value = {
        **_raw_task("new-task"),
        "name": "[Client] Created task",
    }
    client.tasks.stories.return_value = []
    source = AsanaTask.from_raw_task(
        {
            **_raw_task("source"),
            "name": "[Client] Created task",
            "comments": ["Comment after checkpoint"],
        },
    )

    created = side.add_item(source)

    assert created.gid == "new-task"
    client.tasks.add_comment.assert_not_called()


def test_post_create_sync_adds_comments_after_task_identity_exists() -> None:
    side, client = _side()
    client.tasks.stories.side_effect = [[], []]
    source = AsanaTask.from_raw_task(
        {
            **_raw_task("source"),
            "comments": ["Comment after checkpoint"],
        },
    )

    side.post_create_sync("new-task", source)

    client.tasks.add_comment.assert_called_once_with(
        "new-task",
        text="Comment after checkpoint",
    )


def test_structured_comment_cache_without_checked_at_refreshes_once(tmp_path) -> None:
    cache_path = tmp_path / "comments.json"
    cache_path.write_text(
        '{"1":{"version":2,"modified_at":"2026-09-20T12:30:00Z","comments":['
        '{"gid":"story-1","text":"Old text","created_at":"2025-01-02T10:30:00+00:00"}]}}',
    )
    client = MagicMock()
    side = AsanaSide(
        client=client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=cache_path,
    )
    client.tasks.find_all.return_value = [_raw_task("1")]
    client.tasks.search_in_workspace.return_value = []
    client.tasks.stories.return_value = [
        {
            "created_at": "2025-01-02T10:30:00Z",
            "gid": "story-1",
            "type": "comment",
            "text": "Edited text",
        },
    ]

    tasks = side.get_all_items()

    assert [str(comment) for comment in tasks[0].comments] == ["Edited text"]
    client.tasks.stories.assert_called_once()


def test_changed_task_modified_at_invalidates_fresh_comment_cache(tmp_path) -> None:
    cache_path = tmp_path / "comments.json"
    cache_path.write_text(
        '{"1":{"version":2,"modified_at":"2026-09-19T12:30:00Z",'
        '"checked_at":"2026-09-21T12:00:00+00:00","comments":['
        '{"gid":"story-1","text":"Old text","created_at":"2025-01-02T10:30:00+00:00"}]}}',
    )
    client = MagicMock()
    side = AsanaSide(
        client=client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=cache_path,
    )
    client.tasks.find_all.return_value = [_raw_task("1")]
    client.tasks.search_in_workspace.return_value = []
    client.tasks.stories.return_value = [
        {
            "created_at": "2025-01-02T10:30:00Z",
            "gid": "story-1",
            "type": "comment",
            "text": "Current text",
        },
    ]

    tasks = side.get_all_items()

    assert [str(comment) for comment in tasks[0].comments] == ["Current text"]
    client.tasks.stories.assert_called_once()


def test_corrupt_comment_cache_is_rebuilt_from_asana(tmp_path) -> None:
    cache_path = tmp_path / "comments.json"
    cache_path.write_text("{not valid json")
    client = MagicMock()
    side = AsanaSide(
        client=client,
        task_gid=None,
        workspace_gid="workspace-1",
        comment_cache_path=cache_path,
    )
    client.tasks.find_all.return_value = [_raw_task("1")]
    client.tasks.search_in_workspace.return_value = []
    client.tasks.stories.return_value = [
        {
            "created_at": "2025-01-02T10:30:00Z",
            "gid": "story-1",
            "type": "comment",
            "text": "Recovered",
        },
    ]

    tasks = side.get_all_items()

    assert [str(comment) for comment in tasks[0].comments] == ["Recovered"]
    client.tasks.stories.assert_called_once()
