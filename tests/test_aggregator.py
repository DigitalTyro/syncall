from collections.abc import Sequence
from unittest.mock import MagicMock, patch

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

    source_side.record_asana_gid.side_effect = lambda *_: events.append("identity")
    target_side.post_create_sync.side_effect = lambda *_: events.append("comments")
    item = MagicMock()
    item.source_tw_uuid = "tw-source"

    aggregator._get_side_instances = MagicMock(
        return_value=(target_side, source_side),
    )
    aggregator._get_serdes_dirs = MagicMock(return_value=(tmp_path, tmp_path))
    aggregator._summary_of = MagicMock(return_value="Created")
    aggregator._advance_operation_progress = MagicMock()

    created_id = aggregator.inserter_to(item, helper)

    assert created_id == "asana-new"
    assert events == ["identity", "comments"]
    source_side.record_asana_gid.assert_called_once_with("tw-source", "asana-new")


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

    with patch("syncall.aggregator.pickle_dump") as pickle_dump_mock:
        try:
            aggregator.sync()
        except RuntimeError as exc:
            assert "sync state was not committed" in str(exc)
        else:
            raise AssertionError("Expected failed write to abort state commit")

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
