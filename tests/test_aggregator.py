from collections.abc import Sequence
from unittest.mock import MagicMock, patch

import pytest
from bidict import bidict
from item_synchronizer.types import ID
from syncall.aggregator import Aggregator
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
    side_B = MagicMock()
    side_B.get_all_items.return_value = [{"uuid": "b1", "description": "B"}]

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
    aggregator._asana_comment_baseline_version = 1
    aggregator._asana_comment_baseline_key = "append_only_asana_comments_version"
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
    pickle_dump_mock.assert_called_once_with(actual, tmp_path / "tw-1")
    assert ("Tw", "tw-1") in aggregator._written_serdes



def test_collects_only_annotations_new_since_last_tw_snapshot(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_A.other = helper_B
    helper_B.other = helper_A

    asana_side = MagicMock()
    asana_side.ensure_comments = MagicMock()
    tw_side = MagicMock()
    tw_side.new_annotations_since.return_value = ["New local comment"]

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_B = {
        "tw-1": {
            "uuid": "tw-1",
            "annotations": [
                {"entry": "20260923T170000Z", "description": "New local comment"},
            ],
        },
    }
    aggregator._asana_comment_baseline_version = 1
    aggregator._get_serdes_dirs = MagicMock(
        return_value=(tmp_path / "asana", tmp_path / "tw"),
    )
    (tmp_path / "asana").mkdir()
    (tmp_path / "tw").mkdir()
    (tmp_path / "tw" / "tw-1").write_bytes(b"snapshot")

    changes_A = MagicMock()
    changes_A.deleted = set()
    changes_B = MagicMock()
    changes_B.modified = {"tw-1"}

    with patch("syncall.aggregator.pickle_load", return_value={"annotations": []}):
        pending = aggregator._collect_new_asana_comments(changes_A, changes_B)

    assert pending == {"asana-1": ["New local comment"]}
    tw_side.new_annotations_since.assert_called_once()


def test_does_not_collect_annotations_before_upgrade_baseline(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_A.other = helper_B
    helper_B.other = helper_A

    asana_side = MagicMock()
    asana_side.ensure_comments = MagicMock()
    tw_side = MagicMock()

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_B = {
        "tw-1": {
            "uuid": "tw-1",
            "annotations": [
                {"entry": "20260923T170000Z", "description": "Historical local annotation"},
            ],
        },
    }
    aggregator._asana_comment_baseline_version = 0

    changes_A = MagicMock()
    changes_A.deleted = set()
    changes_B = MagicMock()
    changes_B.modified = {"tw-1"}

    pending = aggregator._collect_new_asana_comments(changes_A, changes_B)

    assert pending == {}
    tw_side.new_annotations_since.assert_not_called()


def test_collecting_comments_is_independent_of_whole_task_conflict_winner(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    helper_B = MagicMock()
    helper_B.name = "Tw"
    helper_A.other = helper_B
    helper_B.other = helper_A

    asana_side = MagicMock()
    asana_side.ensure_comments = MagicMock()
    tw_side = MagicMock()
    tw_side.new_annotations_since.return_value = ["Keep this TW comment"]

    aggregator._helper_A = helper_A
    aggregator._helper_B = helper_B
    aggregator._side_A = asana_side
    aggregator._side_B = tw_side
    aggregator._B_to_A_map = bidict({"tw-1": "asana-1"})
    aggregator._items_B = {
        "tw-1": {
            "uuid": "tw-1",
            "annotations": [
                {"entry": "20260923T170000Z", "description": "Keep this TW comment"},
            ],
        },
    }
    aggregator._asana_comment_baseline_version = 1
    aggregator._get_serdes_dirs = MagicMock(
        return_value=(tmp_path / "asana", tmp_path / "tw"),
    )
    (tmp_path / "asana").mkdir()
    (tmp_path / "tw").mkdir()
    (tmp_path / "tw" / "tw-1").write_bytes(b"snapshot")

    # Both sides changed. The generic resolver may later choose Asana, but comment
    # collection happens before that and is therefore not lost with the TW task update.
    changes_A = MagicMock()
    changes_A.modified = {"asana-1"}
    changes_A.deleted = set()
    changes_B = MagicMock()
    changes_B.modified = {"tw-1"}

    with patch("syncall.aggregator.pickle_load", return_value={"annotations": []}):
        pending = aggregator._collect_new_asana_comments(changes_A, changes_B)

    assert pending == {"asana-1": ["Keep this TW comment"]}


def test_append_new_comments_checkpoints_post_comment_asana_state(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    helper_A = MagicMock()
    helper_A.name = "Asana"
    asana_side = MagicMock()
    current = {"gid": "asana-1", "name": "Task", "comments": ("New local comment",)}
    asana_side.get_item.return_value = current

    aggregator._helper_A = helper_A
    aggregator._side_A = asana_side
    aggregator._get_serdes_dirs = MagicMock(
        return_value=(tmp_path / "asana", tmp_path / "tw"),
    )
    (tmp_path / "asana").mkdir()
    (tmp_path / "tw").mkdir()
    aggregator._written_serdes = set()

    with patch("syncall.aggregator.pickle_dump") as pickle_dump_mock:
        aggregator._append_new_asana_comments(
            {"asana-1": ["New local comment"]},
        )

    asana_side.ensure_comments.assert_called_once_with(
        "asana-1",
        ["New local comment"],
    )
    asana_side.get_item.assert_called_once_with("asana-1", use_cached=False)
    pickle_dump_mock.assert_called_once_with(
        current,
        tmp_path / "asana" / "asana-1",
    )
    assert ("Asana", "asana-1") in aggregator._written_serdes

def test_comment_baseline_is_enabled_only_after_successful_sync_state(tmp_path) -> None:
    aggregator = Aggregator.__new__(Aggregator)
    aggregator._asana_comment_baseline_version = 0
    aggregator._asana_comment_baseline_key = "append_only_asana_comments_version"
    aggregator.prefs_manager = {}
    aggregator.flush_correspondences = MagicMock()

    aggregator._enable_append_only_asana_comments_after_baseline()

    assert aggregator.prefs_manager["append_only_asana_comments_version"] == 1
    assert aggregator._asana_comment_baseline_version == 1
    aggregator.flush_correspondences.assert_called_once()
