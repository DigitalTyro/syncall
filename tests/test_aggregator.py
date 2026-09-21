from collections.abc import Sequence
from unittest.mock import MagicMock

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
