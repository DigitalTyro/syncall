from unittest.mock import MagicMock

import pytest
from syncall.scripts.tw_asana_sync import resume_pending_asana_comments


def _pending_task() -> dict:
    return {
        "uuid": "tw-1",
        "asana_gid": "asana-1",
        "asana_pending_comments": "1",
        "description": "Task",
        "entry": "20260920T100000Z",
        "modified": "20260921T100000Z",
        "status": "pending",
        "annotations": ["First", "Second"],
    }


def test_pending_comment_recovery_confirms_remote_before_clearing_marker() -> None:
    tw_side = MagicMock()
    asana_side = MagicMock()
    events: list[str] = []

    def record_remote(*_args) -> None:
        events.append("remote")

    def record_clear(*_args) -> None:
        events.append("clear")

    asana_side.ensure_comments.side_effect = record_remote
    tw_side.clear_pending_asana_comments.side_effect = record_clear

    resumed = resume_pending_asana_comments(
        tw_side,
        asana_side,
        [_pending_task()],
    )

    assert resumed == 1
    assert events == ["remote", "clear"]
    desired_comments = asana_side.ensure_comments.call_args.args[1]
    assert [str(comment) for comment in desired_comments] == ["First", "Second"]
    tw_side.clear_pending_asana_comments.assert_called_once_with("tw-1")


def test_pending_comment_recovery_keeps_marker_when_remote_check_fails() -> None:
    tw_side = MagicMock()
    asana_side = MagicMock()
    asana_side.ensure_comments.side_effect = RuntimeError("Asana unavailable")

    with pytest.raises(RuntimeError, match="Asana unavailable"):
        resume_pending_asana_comments(
            tw_side,
            asana_side,
            [_pending_task()],
        )

    tw_side.clear_pending_asana_comments.assert_not_called()


def test_non_pending_tasks_are_not_used_for_outbound_comment_recovery() -> None:
    tw_side = MagicMock()
    asana_side = MagicMock()
    task = _pending_task()
    task.pop("asana_pending_comments")

    resumed = resume_pending_asana_comments(
        tw_side,
        asana_side,
        [task],
    )

    assert resumed == 0
    asana_side.ensure_comments.assert_not_called()
    tw_side.clear_pending_asana_comments.assert_not_called()
