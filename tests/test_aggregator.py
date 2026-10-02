from collections.abc import Sequence
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from bidict import bidict
from bubop import pickle_dump, pickle_load
from item_synchronizer.helpers import SideChanges
from item_synchronizer.types import ID
from syncall.aggregator import Aggregator
from syncall.progress import make_progress
from syncall.sync_side import ItemType, SyncSide


class MockSide(SyncSide):
    """MockSide class."""

    def __init__(self, name: str, fullname: str, *args, **kargs) -> None:
        self._fullname = fullname
        self._name = name

    def __str__(self) -> str:
        return self._fullname

    def get_all_items(self, **kargs) -> Sequence[ItemType]:
        raise NotImplementedError("Implement in derived")

    def get_item(self, item_id: ID, use_cached: bool = False) -> ItemType | None:
        raise NotImplementedError("Should be implemented in derived")

    def delete_single_item(self, item_id: ID):
        raise NotImplementedError("Should be implemented in derived")

    def update_item(self, item_id: ID, **changes):
        raise NotImplementedError("Should be implemented in derived")

    def add_item(self, item: ItemType) -> ItemType:
        raise NotImplementedError("Implement in derived")

    @classmethod
    def id_key(cls) -> str:
        raise NotImplementedError("Implement in derived")

    @classmethod
    def summary_key(cls) -> str:
        raise NotImplementedError("Implement in derived")

    @classmethod
    def items_are_identical(
        cls,
        item1: ItemType,
        item2: ItemType,
        ignore_keys: Sequence[str] = [],
    ) -> bool:
        """Determine whether two items are identical.

        .. returns:: True if items are identical, False otherwise.
        """
        raise NotImplementedError("Implement in derived")


def test_missing_filtered_item_that_still_exists_is_not_deleted(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    side = MagicMock()
    side.get_item.return_value = {"id": "1"}

    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._get_ids_map = MagicMock(return_value={"1": "other-id"})
    aggregator._get_side_instances = MagicMock(return_value=(side, MagicMock()))

    changes = aggregator.detect_changes(helper, {})

    assert changes.deleted == set()
    side.get_item.assert_called_once_with("1")


def test_missing_filtered_item_that_is_gone_is_deleted(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    side = MagicMock()
    side.get_item.return_value = None

    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._get_ids_map = MagicMock(return_value={"1": "other-id"})
    aggregator._get_side_instances = MagicMock(return_value=(side, MagicMock()))

    changes = aggregator.detect_changes(helper, {})

    assert changes.deleted == {"1"}
    side.get_item.assert_called_once_with("1")


def test_item_getter_uses_current_snapshot_before_live_side() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_B = MagicMock()
    side_A = MagicMock()
    side_B = MagicMock()
    cached_item = {"id": "asana-1"}

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._items_A = {"asana-1": cached_item}
    aggregator._items_B = {}
    aggregator._get_side_instances = MagicMock(return_value=(side_A, side_B))

    item = aggregator.item_getter_for("asana-1", helper_A)

    assert item is cached_item
    side_A.get_item.assert_not_called()


def test_count_sync_operations_counts_conflict_once() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    changes_A = MagicMock()
    changes_A.new = {"asana-new"}
    changes_A.modified = {"asana-1"}
    changes_A.deleted = set()

    changes_B = MagicMock()
    changes_B.new = {"tw-new"}
    changes_B.modified = {"tw-1"}
    changes_B.deleted = set()

    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})

    assert aggregator._count_sync_operations(changes_A, changes_B) == 3


def test_recover_correspondences_rebuilds_missing_mapping() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    aggregator._B_to_A_map = bidict()

    recovered = aggregator.recover_correspondences(
        {"tw-1": "asana-1", "tw-2": "asana-2"},
    )

    assert recovered == 2
    assert aggregator._B_to_A_map == bidict(
        {"tw-1": "asana-1", "tw-2": "asana-2"},
    )


def test_recover_correspondences_is_idempotent() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})

    recovered = aggregator.recover_correspondences({"tw-1": "asana-1"})

    assert recovered == 0
    assert aggregator._B_to_A_map == bidict({"tw-1": "asana-1"})


def test_recover_correspondences_refuses_conflicting_identity() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})

    try:
        aggregator.recover_correspondences({"tw-1": "asana-other"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected conflicting recovered identity to abort")


def test_asana_create_checkpoints_source_identity_before_comments(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.id_key = "gid"
    helper.summary_key = "name"
    helper.other = MagicMock()
    target_side = MagicMock()
    source_side = MagicMock()
    created = {"gid": "asana-new", "name": "Created"}
    target_side.add_item.return_value = created
    events: list[str] = []

    def record_identity(*_args) -> bool:
        events.append("identity")
        return True

    def record_comments(*_args) -> None:
        events.append("comments")

    def record_clear(*_args) -> None:
        events.append("clear")

    def record_prefs() -> None:
        events.append("prefs")

    source_side.record_asana_gid.side_effect = record_identity
    target_side.post_create_sync.side_effect = record_comments
    source_side.clear_pending_asana_comments.side_effect = record_clear
    item = MagicMock()
    item.source_tw_uuid = "tw-source"

    aggregator._B_to_A_map = bidict()
    aggregator.flush_correspondences = MagicMock(side_effect=record_prefs)
    aggregator._get_side_instances = MagicMock(
        return_value=(target_side, source_side),
    )
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._summary_of = MagicMock(return_value="Created")
    aggregator._advance_operation_progress = MagicMock()
    aggregator._operation_failed = False
    aggregator._written_serdes = set()

    created_id = aggregator.inserter_to(item, helper)

    assert created_id == "asana-new"
    assert events == ["prefs", "identity", "comments", "clear"]
    assert aggregator._B_to_A_map["tw-source"] == "asana-new"
    source_side.record_asana_gid.assert_called_once_with("tw-source", "asana-new")
    source_side.clear_pending_asana_comments.assert_called_once_with("tw-source")


def _minimal_sync_aggregator(tmp_path) -> Aggregator:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.id_key = "gid"
    helper_A.name = "Asana"
    helper_B = MagicMock()
    helper_B.id_key = "uuid"
    helper_B.name = "Tw"
    helper_A.other = helper_B
    helper_B.other = helper_A

    side_A = MagicMock()
    side_A.get_all_items.return_value = [{"gid": "a1", "name": "A"}]
    # This generic fixture intentionally models sides without the optional Asana/TW
    # comment-reconciliation capabilities. Bare MagicMocks manufacture arbitrary callable
    # attributes, which would otherwise make Aggregator._sync_comment_histories() think these
    # unrelated test doubles support the comment protocol.
    side_A.get_comments_live = None
    side_A.ensure_comments = None

    side_B = MagicMock()
    side_B.get_all_items.return_value = [{"uuid": "b1", "description": "B"}]
    side_B.reconcile_asana_comment_state = None

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = side_A
    aggregator._side_B = side_B
    aggregator._items_A = {}
    aggregator._items_B = {}
    aggregator._operation_progress = None
    aggregator._operation_progress_task = None
    aggregator._operation_failed = False
    aggregator._B_to_A_map = bidict({"b1": "a1"})
    aggregator._synchronizer = MagicMock()
    aggregator._get_serdes_dirs = MagicMock(
        return_value=(tmp_path / "a", tmp_path / "b"),
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    aggregator._remove_serdes_files = MagicMock()
    aggregator.flush_correspondences = MagicMock()
    aggregator.prefs_manager = {}
    return aggregator


def test_sync_does_not_commit_source_snapshot_before_writes_succeed(tmp_path) -> None:
    aggregator = _minimal_sync_aggregator(tmp_path)
    changes_A = MagicMock()
    changes_A.new = set()
    changes_A.modified = {"a1"}
    changes_A.deleted = set()
    changes_B = MagicMock()
    changes_B.new = set()
    changes_B.modified = set()
    changes_B.deleted = set()
    aggregator.detect_changes = MagicMock(side_effect=[changes_A, changes_B])
    aggregator._count_sync_operations = MagicMock(return_value=1)

    def fail_after_write_attempt(**_kwargs) -> None:
        aggregator._operation_failed = True

    aggregator._synchronizer.sync.side_effect = fail_after_write_attempt

    with (
        patch("syncall.aggregator.pickle_dump") as pickle_dump_mock,
        pytest.raises(RuntimeError, match="sync state was not committed"),
    ):
        aggregator.sync()

    pickle_dump_mock.assert_not_called()

    aggregator.flush_correspondences.assert_called_once()


def test_sync_failure_includes_the_underlying_taskwarrior_error(tmp_path) -> None:
    aggregator = _minimal_sync_aggregator(tmp_path)
    changes_A = MagicMock()
    changes_A.new = set()
    changes_A.modified = {"a1"}
    changes_A.deleted = set()
    changes_B = MagicMock()
    changes_B.new = set()
    changes_B.modified = set()
    changes_B.deleted = set()
    aggregator.detect_changes = MagicMock(side_effect=[changes_A, changes_B])
    aggregator._count_sync_operations = MagicMock(return_value=1)

    class _TaskwarriorError(Exception):
        def __init__(self) -> None:
            super().__init__("task modify ... est:3:00:00")
            self.stderr = b"The duration value '3:00:00' is not supported."

    def fail_after_write_attempt(**_kwargs) -> None:
        aggregator._operation_failed = True
        aggregator._operation_errors = [_TaskwarriorError()]

    aggregator._synchronizer.sync.side_effect = fail_after_write_attempt

    with pytest.raises(RuntimeError, match="duration value '3:00:00' is not supported"):
        aggregator.sync()


def test_sync_commits_source_snapshot_only_after_successful_writes(tmp_path) -> None:
    aggregator = _minimal_sync_aggregator(tmp_path)
    changes_A = MagicMock()
    changes_A.new = set()
    changes_A.modified = {"a1"}
    changes_A.deleted = set()
    changes_B = MagicMock()
    changes_B.new = set()
    changes_B.modified = set()
    changes_B.deleted = set()
    aggregator.detect_changes = MagicMock(side_effect=[changes_A, changes_B])
    aggregator._count_sync_operations = MagicMock(return_value=1)

    with patch("syncall.aggregator.pickle_dump") as pickle_dump_mock:

        def successful_write(**_kwargs) -> None:
            pickle_dump_mock.assert_not_called()

        aggregator._synchronizer.sync.side_effect = successful_write
        aggregator.sync()

        pickle_dump_mock.assert_called_once()

    aggregator.flush_correspondences.assert_called_once()


def test_missing_snapshot_is_safely_baselined_without_remote_change(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.name = "Asana"
    side = MagicMock()
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._get_ids_map = MagicMock(return_value={"a1": "b1"})
    aggregator._get_side_instances = MagicMock(return_value=(side, MagicMock()))
    aggregator._item_has_update = MagicMock()

    item = {"gid": "a1", "name": "Current remote state"}
    changes = aggregator.detect_changes(helper, {"a1": item})

    assert changes.modified == set()
    assert changes.deleted == set()
    assert (tmp_path / "a1").is_file()
    aggregator._item_has_update.assert_not_called()
    side.get_item.assert_not_called()


def test_authoritative_written_snapshot_is_not_overwritten_by_pre_sync_cache(tmp_path) -> None:
    aggregator = _minimal_sync_aggregator(tmp_path)
    changes_A = MagicMock()
    changes_A.new = set()
    changes_A.modified = {"a1"}
    changes_A.deleted = set()
    changes_B = MagicMock()
    changes_B.new = set()
    changes_B.modified = set()
    changes_B.deleted = set()
    aggregator.detect_changes = MagicMock(side_effect=[changes_A, changes_B])
    aggregator._count_sync_operations = MagicMock(return_value=1)

    def write_authoritative_snapshot(**_kwargs) -> None:
        aggregator._written_serdes.add(("Asana", "a1"))

    aggregator._synchronizer.sync.side_effect = write_authoritative_snapshot

    with patch("syncall.aggregator.pickle_dump") as pickle_dump_mock:
        aggregator.sync()

        pickle_dump_mock.assert_not_called()


def test_new_asana_task_comments_continue_if_one_identity_checkpoint_succeeds(
    tmp_path,
) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.id_key = "gid"
    helper.name = "Asana"
    helper.summary_key = "name"
    helper.other = MagicMock()
    target_side = MagicMock()
    source_side = MagicMock()
    target_side.add_item.return_value = {"gid": "asana-new", "name": "Created"}
    source_side.record_asana_gid.return_value = False
    item = MagicMock()
    item.source_tw_uuid = "tw-source"

    aggregator._B_to_A_map = bidict()
    aggregator.flush_correspondences = MagicMock()
    aggregator._get_side_instances = MagicMock(
        return_value=(target_side, source_side),
    )
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._summary_of = MagicMock(return_value="Created")
    aggregator._advance_operation_progress = MagicMock()
    aggregator._operation_failed = False
    aggregator._written_serdes = set()

    created_id = aggregator.inserter_to(item, helper)

    assert created_id == "asana-new"
    target_side.post_create_sync.assert_called_once_with("asana-new", item)


def test_new_asana_task_refuses_comments_if_identity_cannot_be_checkpointed(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.id_key = "gid"
    helper.name = "Asana"
    helper.summary_key = "name"
    helper.other = MagicMock()
    target_side = MagicMock()
    source_side = MagicMock()
    target_side.add_item.return_value = {"gid": "asana-new", "name": "Created"}
    source_side.record_asana_gid.return_value = False
    item = MagicMock()
    item.source_tw_uuid = "tw-source"

    aggregator._B_to_A_map = bidict()
    aggregator.flush_correspondences = MagicMock(
        side_effect=OSError("Preferences write failed"),
    )
    aggregator._get_side_instances = MagicMock(
        return_value=(target_side, source_side),
    )
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._summary_of = MagicMock(return_value="Created")
    aggregator._advance_operation_progress = MagicMock()
    aggregator._operation_failed = False
    aggregator._written_serdes = set()

    with pytest.raises(RuntimeError, match="refusing to create comments"):
        aggregator.inserter_to(item, helper)

    target_side.post_create_sync.assert_not_called()
    assert aggregator._operation_failed is True


def test_failed_update_checkpoints_actual_target_for_safe_retry(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.name = "Asana"
    helper.summary_key = "name"
    helper.other = MagicMock()
    side = MagicMock()
    side.update_item.side_effect = RuntimeError("partial remote failure")
    current_target = {"gid": "a1", "name": "Partially updated remote"}
    side.get_item.return_value = current_target

    aggregator._get_side_instances = MagicMock(return_value=(side, MagicMock()))
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._summary_of = MagicMock(return_value="Task")
    aggregator._operation_failed = False
    aggregator._written_serdes = set()
    aggregator._advance_operation_progress = MagicMock()

    with (
        patch("syncall.aggregator.pickle_dump") as pickle_dump_mock,
        pytest.raises(RuntimeError, match="partial remote failure"),
    ):
        aggregator.updater_to("a1", {"name": "Desired"}, helper)

    pickle_dump_mock.assert_called_once_with(current_target, tmp_path / "a1")

    side.get_item.assert_called_once_with("a1", use_cached=False)
    assert aggregator._operation_failed is True
    assert ("Asana", "a1") in aggregator._written_serdes
    aggregator._advance_operation_progress.assert_called_once()


def test_successful_update_caches_actual_readback_state(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    helper.name = "Tw"
    helper.summary_key = "description"
    helper.other = MagicMock()
    side = MagicMock()
    actual = {"uuid": "tw-1", "description": "Stored form"}
    side.get_item.return_value = actual

    aggregator._get_side_instances = MagicMock(return_value=(side, MagicMock()))
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._summary_of = MagicMock(return_value="Task")
    aggregator._operation_failed = False
    aggregator._written_serdes = set()
    aggregator._advance_operation_progress = MagicMock()

    with patch("syncall.aggregator.pickle_dump") as pickle_dump_mock:
        aggregator.updater_to(
            "tw-1",
            {"description": "Intended form"},
            helper,
        )

    side.get_item.assert_called_once_with("tw-1", use_cached=False)
    aggregator._advance_operation_progress.assert_called_once()
    pickle_dump_mock.assert_called_once_with(actual, tmp_path / "tw-1")
    assert ("Tw", "tw-1") in aggregator._written_serdes


def test_skipped_conversion_counts_as_a_finished_operation() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    aggregator._advance_operation_progress = MagicMock()

    assert aggregator._count_skipped_conversion(lambda _item: None, {"gid": "a1"}) is None

    aggregator._advance_operation_progress.assert_called_once()


def test_successful_conversion_is_counted_by_the_following_write() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    aggregator._advance_operation_progress = MagicMock()

    assert aggregator._count_skipped_conversion(
        lambda _item: {"uuid": "tw-1"}, {"gid": "a1"}
    ) == {
        "uuid": "tw-1",
    }

    aggregator._advance_operation_progress.assert_not_called()


def test_applying_sync_changes_reaches_the_counted_total(tmp_path, monkeypatch) -> None:
    aggregator = _minimal_sync_aggregator(tmp_path)
    changes_a = MagicMock()
    changes_a.new = set()
    changes_a.modified = set()
    changes_a.deleted = set()
    changes_b = MagicMock()
    changes_b.new = set()
    changes_b.modified = set()
    changes_b.deleted = set()
    aggregator.detect_changes = MagicMock(side_effect=[changes_a, changes_b])
    aggregator._count_sync_operations = MagicMock(return_value=12)
    captured: dict[str, object] = {}

    def fake_make_progress(*, console, unit):
        progress = make_progress(console=console, unit=unit)
        progress.disable = True
        captured["progress"] = progress
        return progress

    def one_tick(**_kwargs) -> None:
        aggregator._advance_operation_progress()

    aggregator._synchronizer.sync.side_effect = one_tick
    monkeypatch.setattr("syncall.aggregator.make_progress", fake_make_progress)

    aggregator.sync()

    progress = captured["progress"]
    task = progress.tasks[0]
    assert task.completed == 12
    assert task.total == 12


def test_sync_comment_histories_backfills_missed_local_comment() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_A.id_key = "gid"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_B.id_key = "uuid"
    helper_A.other = helper_B
    helper_B.other = helper_A

    asana_item = MagicMock()
    asana_item.comments = ()
    asana_side = MagicMock()
    asana_side.get_comments_live.side_effect = [
        ("Already remote",),
        ("Already remote", "Missed local comment"),
    ]
    tw_side = MagicMock()
    tw_side.reconcile_asana_comment_state.side_effect = [
        ["Missed local comment"],
        [],
    ]
    tw_side.get_item.return_value = {
        "uuid": "tw-1",
        "annotations": [
            {"entry": "20260923T170000Z", "description": "Missed local comment"},
        ],
    }

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_A = {"asana-1": asana_item}
    aggregator._items_B = {"tw-1": {"uuid": "tw-1"}}
    aggregator._live_asana_comments = {}
    aggregator._check_comments = True

    aggregator._sync_comment_histories()

    asana_side.ensure_comments.assert_called_once_with(
        "asana-1",
        ["Missed local comment"],
    )
    assert tw_side.reconcile_asana_comment_state.call_count == 2
    assert asana_item.comments == ("Already remote", "Missed local comment")


def test_sync_comment_histories_imports_remote_comment_without_generic_cache() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_A.id_key = "gid"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_B.id_key = "uuid"
    helper_A.other = helper_B
    helper_B.other = helper_A

    asana_item = MagicMock()
    asana_item.comments = ()
    asana_side = MagicMock()
    asana_side.get_comments_live.return_value = ("New remote comment",)
    tw_side = MagicMock()
    tw_side.reconcile_asana_comment_state.return_value = []

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_A = {"asana-1": asana_item}
    aggregator._items_B = {"tw-1": {"uuid": "tw-1"}}
    aggregator._live_asana_comments = {}
    aggregator._check_comments = True

    aggregator._sync_comment_histories()

    tw_side.reconcile_asana_comment_state.assert_called_once_with(
        "tw-1",
        aggregator._items_B["tw-1"],
        ("New remote comment",),
        trust_remote_absence=True,
    )
    asana_side.ensure_comments.assert_not_called()
    assert asana_item.comments == ("New remote comment",)


def test_sync_comment_histories_fails_if_append_does_not_converge() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_A.other = helper_B
    helper_B.other = helper_A

    asana_item = MagicMock()
    asana_item.comments = ()
    asana_side = MagicMock()
    asana_side.get_comments_live.side_effect = [(), ("Still not bound",)]
    tw_side = MagicMock()
    tw_side.reconcile_asana_comment_state.side_effect = [
        ["Local comment"],
        ["Local comment"],
    ]
    tw_side.get_item.return_value = {"uuid": "tw-1"}

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_A = {"asana-1": asana_item}
    aggregator._items_B = {"tw-1": {"uuid": "tw-1"}}
    aggregator._live_asana_comments = {}

    with pytest.raises(RuntimeError, match="still has unsynchronized comments"):
        aggregator._sync_comment_histories()


def test_comment_sync_does_not_use_serdes_or_global_baseline() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_A.other = helper_B
    helper_B.other = helper_A
    asana_side = MagicMock()
    asana_side.get_comments_live.return_value = ()
    tw_side = MagicMock()
    tw_side.reconcile_asana_comment_state.return_value = []

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_A = {"asana-1": {}}
    aggregator._items_B = {"tw-1": {}}
    aggregator._live_asana_comments = {}

    aggregator._sync_comment_histories()

    tw_side.reconcile_asana_comment_state.assert_called_once()
    asana_side.get_comments_live.assert_not_called()


def test_default_comment_sync_uses_loaded_comments_without_rereading_asana() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_A.other = helper_B
    helper_B.other = helper_A

    asana_item = MagicMock()
    asana_item.comments = ("Cached comment",)
    asana_side = MagicMock()
    asana_side.comment_history_fetched.return_value = False
    tw_side = MagicMock()
    tw_side.reconcile_asana_comment_state.return_value = []

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_A = {"asana-1": asana_item}
    aggregator._items_B = {"tw-1": {"uuid": "tw-1"}}
    aggregator._live_asana_comments = {}

    aggregator._sync_comment_histories()

    asana_side.get_comments_live.assert_not_called()
    tw_side.reconcile_asana_comment_state.assert_called_once_with(
        "tw-1",
        aggregator._items_B["tw-1"],
        ("Cached comment",),
        trust_remote_absence=False,
    )


def test_fresh_story_fetch_allows_comment_deletion_without_full_reread() -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_A.other = helper_B
    helper_B.other = helper_A

    asana_item = MagicMock()
    asana_item.comments = ()
    asana_side = MagicMock()
    asana_side.comment_history_fetched.return_value = True
    tw_side = MagicMock()
    tw_side.reconcile_asana_comment_state.return_value = []

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_A = {"asana-1": asana_item}
    aggregator._items_B = {"tw-1": {"uuid": "tw-1"}}
    aggregator._live_asana_comments = {}

    aggregator._sync_comment_histories()

    asana_side.get_comments_live.assert_not_called()
    tw_side.reconcile_asana_comment_state.assert_called_once_with(
        "tw-1",
        aggregator._items_B["tw-1"],
        (),
        trust_remote_absence=True,
    )


def _completion_aggregator(tmp_path, *, asana_item: dict, tw_item: dict):
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_B = MagicMock()
    helper_A.other = helper_B
    helper_B.other = helper_A
    asana_dir = tmp_path / "asana"
    tw_dir = tmp_path / "tw"
    asana_dir.mkdir()
    tw_dir.mkdir()
    pickle_dump(asana_item, asana_dir / "asana-1")
    pickle_dump(tw_item, tw_dir / "tw-1")
    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = MagicMock()
    aggregator._side_B = MagicMock()
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_A = {"asana-1": asana_item}
    aggregator._items_B = {"tw-1": tw_item}
    aggregator._baselined_item_ids = {}
    aggregator._get_serdes_dirs = MagicMock(return_value=(asana_dir, tw_dir))
    aggregator.change_log = None
    return aggregator, asana_dir, tw_dir


def _pending_completion_pair():
    completed_at = datetime(2026, 9, 25, 8, 53, 55, tzinfo=UTC)
    asana_item = {
        "gid": "asana-1",
        "name": "[Juice] Consolidate duplicate power bank collections",
        "completed": True,
        "completed_at": completed_at,
    }
    tw_item = {
        "uuid": "tw-1",
        "description": "Consolidate duplicate power bank collections",
        "status": "pending",
        "end": None,
    }
    return completed_at, asana_item, tw_item


def test_stored_completion_repair_uses_asana_completion_time(tmp_path) -> None:
    completed_at, asana_item, tw_item = _pending_completion_pair()
    aggregator, _asana_dir, tw_dir = _completion_aggregator(
        tmp_path,
        asana_item=asana_item,
        tw_item=tw_item,
    )
    stored = {**tw_item, "status": "completed", "end": completed_at}
    aggregator._side_B.get_item.return_value = stored

    repairs = aggregator._stored_completion_repairs(SideChanges(), SideChanges())
    aggregator._apply_stored_completion_repairs(repairs)

    assert repairs == [("tw-1", "asana-1")]
    aggregator._side_B.update_item.assert_called_once_with(
        "tw-1",
        status="completed",
        end=completed_at,
    )
    aggregator._side_A.update_item.assert_not_called()
    assert pickle_load(tw_dir / "tw-1")["status"] == "completed"
    assert pickle_load(tw_dir / "tw-1")["end"] == completed_at
    assert aggregator._items_B["tw-1"]["status"] == "completed"


def test_stored_completion_repair_leaves_real_status_edits_alone(tmp_path) -> None:
    _completed_at, asana_item, tw_item = _pending_completion_pair()
    aggregator, _asana_dir, _tw_dir = _completion_aggregator(
        tmp_path,
        asana_item=asana_item,
        tw_item=tw_item,
    )

    assert (
        aggregator._stored_completion_repairs(SideChanges(), SideChanges(modified={"tw-1"}))
        == []
    )
    aggregator._items_A.clear()
    assert aggregator._stored_completion_repairs(SideChanges(), SideChanges()) == []


def test_stored_completion_repair_skips_a_snapshot_created_this_run(tmp_path) -> None:
    _completed_at, asana_item, tw_item = _pending_completion_pair()
    aggregator, _asana_dir, _tw_dir = _completion_aggregator(
        tmp_path,
        asana_item=asana_item,
        tw_item=tw_item,
    )
    aggregator._baselined_item_ids = {id(aggregator._helper_A): {"asana-1"}}

    assert aggregator._stored_completion_repairs(SideChanges(), SideChanges()) == []


def test_stored_completion_repair_requires_asana_completion_time(tmp_path) -> None:
    _completed_at, asana_item, tw_item = _pending_completion_pair()
    asana_item["completed_at"] = None
    aggregator, _asana_dir, _tw_dir = _completion_aggregator(
        tmp_path,
        asana_item=asana_item,
        tw_item=tw_item,
    )

    assert aggregator._stored_completion_repairs(SideChanges(), SideChanges()) == []
    aggregator._side_B.update_item.assert_not_called()


def test_missing_snapshot_is_baselined_without_a_change(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper = MagicMock()
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._get_ids_map = MagicMock(return_value={"1": "other"})
    aggregator._get_side_instances = MagicMock(return_value=(MagicMock(), MagicMock()))
    aggregator._item_has_update = MagicMock(return_value=True)

    changes = aggregator.detect_changes(helper, {"1": {"id": "1", "status": "pending"}})

    assert changes.modified == set()
    assert aggregator._baselined_item_ids[id(helper)] == {"1"}
    aggregator._item_has_update.assert_not_called()
