from __future__ import annotations

import datetime
from unittest.mock import MagicMock

from bidict import bidict
from bubop import pickle_dump
from syncall.aggregator import Aggregator
from syncall.asana.asana_side import AsanaSide
from syncall.asana.asana_task import AsanaTask
from syncall.tw_asana_utils import convert_tw_to_asana


def _asana_task(*, completed: bool = True, due_on: datetime.date | None = None) -> AsanaTask:
    return AsanaTask.from_raw_task(
        {
            "completed": completed,
            "completed_at": "2026-09-22T16:09:42Z" if completed else None,
            "created_at": "2026-01-01T00:00:00Z",
            "due_at": None,
            "due_on": due_on.isoformat() if due_on is not None else None,
            "gid": "asana-1",
            "html_notes": "<body>Keep me</body>",
            "modified_at": "2026-09-23T12:00:00Z",
            "name": "[Spectrum] Create social value pages",
        },
    )


def _aggregator(tmp_path, *, current: dict, snapshot: dict, live: AsanaTask) -> Aggregator:
    aggregator = Aggregator.__new__(Aggregator)
    aggregator._helper_B = MagicMock()
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_A = {"asana-1": live}
    aggregator._items_B = {"tw-1": current}
    aggregator._side_A = AsanaSide(client=MagicMock(), task_gid=None, workspace_gid="ws")
    aggregator._plain_converter_B_to_A = convert_tw_to_asana
    aggregator._operation_progress = None
    aggregator._operation_progress_task = None
    pickle_dump(snapshot, tmp_path / "tw-1")
    return aggregator


def test_comment_or_notes_bookkeeping_does_not_reset_asana_completion(tmp_path) -> None:
    current = {
        "uuid": "tw-1",
        "description": "Create social value pages",
        "client": "Spectrum",
        "status": "pending",
        "due": None,
        "entry": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        "modified": datetime.datetime(2026, 9, 24, 18, 0, tzinfo=datetime.UTC),
        "notes": "reprojected notes",
        "annotations": [{"entry": "20260921T120000Z", "description": "Local only"}],
    }
    snapshot = {**current, "notes": "old notes", "annotations": []}
    aggregator = _aggregator(
        tmp_path,
        current=current,
        snapshot=snapshot,
        live=_asana_task(completed=True),
    )
    converted = convert_tw_to_asana(current)

    assert aggregator._limit_asana_update_to_tw_changes("tw-1", converted) is None
    assert aggregator._convert_existing_tw_to_asana(current) is None


def test_real_tw_due_change_does_not_rewrite_asana_completion(tmp_path) -> None:
    current = {
        "uuid": "tw-1",
        "description": "Create social value pages",
        "client": "Spectrum",
        "status": "pending",
        "due": datetime.datetime(2026, 9, 30, tzinfo=datetime.UTC),
        "entry": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        "modified": datetime.datetime(2026, 9, 24, 18, 0, tzinfo=datetime.UTC),
        "notes": "notes",
    }
    snapshot = {**current, "due": datetime.datetime(2026, 9, 22, tzinfo=datetime.UTC)}
    live = _asana_task(completed=True, due_on=datetime.date(2026, 9, 22))
    aggregator = _aggregator(tmp_path, current=current, snapshot=snapshot, live=live)
    converted = convert_tw_to_asana(current)

    limited = aggregator._limit_asana_update_to_tw_changes("tw-1", converted)

    assert limited is not None
    assert limited.completed is True
    assert limited.due_on == datetime.date(2026, 9, 30)
    assert limited.html_notes == "<body>Keep me</body>"
