from __future__ import annotations

import copy
import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from bubop import logger, parse_datetime
from taskw_ng import TaskWarrior
from taskw_ng.warrior import TASKRC
from xdg import xdg_config_home

from syncall.sync_side import ItemType, SyncSide

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from syncall.types import TaskwarriorRawItem

tw_duration_key = "syncallduration"
tw_client_key = "client"
tw_notes_key = "notes"

OrderByType = Literal[
    "description",
    "end",
    "entry",
    "id",
    "modified",
    "status",
    "urgency",
]

TW_CONFIG_DEFAULT_OVERRIDES = {
    "context": "none",
    "uda": {
        tw_duration_key: {"type": "duration", "label": "Syncall Duration"},
        tw_client_key: {"type": "string", "label": "Client"},
        tw_notes_key: {"type": "string", "label": "Notes"},
    },
}


def parse_datetime_(dt: str | datetime.datetime) -> datetime.datetime:
    if isinstance(dt, datetime.datetime):
        return dt

    return parse_datetime(dt)


def merge_config_overrides(config_overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(TW_CONFIG_DEFAULT_OVERRIDES)
    for key, value in config_overrides.items():
        if key == "uda" and isinstance(value, Mapping):
            merged["uda"].update(value)
        else:
            merged[key] = value
    return merged


class TaskWarriorSide(SyncSide):
    """Handles interaction with the TaskWarrior client."""

    ID_KEY = "uuid"
    SUMMARY_KEY = "description"
    LAST_MODIFICATION_KEY = "modified"

    def __init__(
        self,
        tags: Sequence[str] = (),
        project: str | None = None,
        tw_filter: str = "",
        config_file_override: Path | None = None,
        config_overrides: Mapping[str, Any] = {},
        **kargs,
    ):
        """Init.

        :param tags: Only include tasks that have are tagged using *all* the specified tags.
                     Also assign these tags to newly added items
        :param project: Only include tasks that include in this project. Also assign newly
                        added items to this project.
        :param tw_filter: Arbitrary taskwarrior filter to use for determining the list of tasks
                          to sync
        :param config_file: Path to the taskwarrior RC file
        :param config_overrides: Dictionary of taskrc key, values to override. See also
                                 TW_CONFIG_DEFAULT_OVERRIDES
        """
        super().__init__(name="Tw", fullname="Taskwarrior", **kargs)
        self._tags: set[str] = set(tags)
        self._project: str = project or ""
        self._tw_filter: str = tw_filter

        config_overrides_ = merge_config_overrides(config_overrides)

        config_file = None
        candidate_config_files = [
            Path(TASKRC).expanduser(),
            xdg_config_home() / "task" / "taskrc",
        ]
        if config_file_override is not None:
            if not config_file_override.is_file():
                raise FileNotFoundError(config_file_override)
            config_file = config_file_override
        else:
            for candidate in candidate_config_files:
                if candidate.is_file():
                    config_file = candidate

        if config_file is None:
            raise RuntimeError(
                "Could not determine a valid taskwarrior config file and no override config"
                " file was specified - candidates:"
                f" {', '.join([str(p) for p in candidate_config_files])}",
            )
        logger.debug(f"Initializing Taskwarrior instance using config file: {config_file}")

        self._tw = TaskWarrior(
            marshal=True,
            config_filename=str(config_file),
            config_overrides=config_overrides_,
        )

        self._items_cache: dict[str, TaskwarriorRawItem] = {}
        self._reload_items = True

    def start(self):
        logger.info(f"Initializing {self.fullname}...")

    def _load_all_items(self):
        """Load all tasks to memory."""
        if not self._reload_items:
            return
        filter_ = [*[f"+{tag}" for tag in self._tags]]
        if self._tw_filter:
            filter_.append(self._tw_filter)
        if self._project:
            filter_.append(f"pro:{self._project}")
        filter_ = f"( {' and '.join(filter_)} )"
        logger.debug(f"Using the following filter to fetch TW tasks: {filter_}")
        tasks = self._tw.load_tasks_and_filter(command="all", filter_=filter_)

        items = [*tasks["completed"], *tasks["pending"]]
        self._items_cache = {str(item["uuid"]): item for item in items}  # type: ignore
        self._reload_items = False

    def get_all_items(
        self,
        skip_completed=False,
        order_by: OrderByType | None = None,
        use_ascending_order: bool = True,
        **kargs,
    ) -> list[TaskwarriorRawItem]:
        self._load_all_items()
        tasks = list(self._items_cache.values())
        if skip_completed:
            tasks = [t for t in tasks if t["status"] != "completed"]  # type: ignore

        for task in tasks:
            task["uuid"] = str(task["uuid"])  # type: ignore

        if order_by is not None:
            tasks.sort(key=lambda t: t[kargs["order_by"]], reverse=not use_ascending_order)  # type: ignore

        return tasks

    def get_item(self, item_id: str, use_cached: bool = True) -> TaskwarriorRawItem | None:
        item = self._items_cache.get(item_id)
        if not use_cached or item is None:
            item = self._tw.get_task(id=item_id)[-1]
            if item is None:
                return None

            self._items_cache[str(item["uuid"])] = item  # type: ignore
        item["uuid"] = str(item["uuid"])
        return item if item["status"] != "deleted" else None  # type: ignore

    def update_item(self, item_id: str, **changes):
        changes.pop("id", False)
        t = self._tw.get_task(uuid=UUID(item_id))[-1]

        unwanted_keys = ["imask", "recur", "rtype", "parent", "urgency"]
        for i in unwanted_keys:
            t.pop(i, False)

        d = dict(t)
        d.update(changes)
        self._tw.task_update(d)

    def add_item(self, item: ItemType) -> ItemType:
        item = cast("TaskwarriorRawItem", item)
        assert "description" in item.keys(), "Item doesn't have a description."
        assert "uuid" not in item.keys(), (
            "Item already has a UUID, try updating it instead of adding it"
        )

        curr_status = item.get("status", None)
        if curr_status not in ["pending", "done", "completed"]:
            logger.warning(f"Invalid status of task [{item['status']}], setting it to pending")  # type: ignore
            item["status"] = "pending"

        if self._tags:
            item["tags"] = list(self._tags.union(item.get("tags", {})))
        if self._project:
            item["project"] = self._project

        description = item.pop("description")
        len_print = min(20, len(description))

        logger.trace(f'Adding task "{description[0:len_print]}" with properties:\n\n{item}')
        new_item = self._tw.task_add(description=description, **item)  # type: ignore
        new_id = new_item["id"]
        logger.debug(f'Task "{new_id}" created - "{description[0:len_print]}"...')

        if curr_status == "deleted":
            logger.debug(
                f'Task "{new_id}" marking as deleted - "{description[0:len_print]}"...',
            )
            self._tw.task_delete(id=new_id)

        return cast("ItemType", new_item)

    def delete_single_item(self, item_id) -> None:
        self._tw.task_delete(uuid=item_id)

    @classmethod
    def id_key(cls) -> str:
        return cls.ID_KEY

    @classmethod
    def summary_key(cls) -> str:
        return cls.SUMMARY_KEY

    @classmethod
    def last_modification_key(cls) -> str:
        return cls.LAST_MODIFICATION_KEY

    @classmethod
    def items_are_identical(
        cls,
        item1: dict,
        item2: dict,
        ignore_keys: Sequence[str] = [],
    ) -> bool:
        item1 = item1.copy()
        item2 = item2.copy()

        keys = [
            k
            for k in [
                "annotations",
                tw_client_key,
                "description",
                "scheduled",
                "due",
                tw_notes_key,
                "status",
                "uuid",
                tw_duration_key,
            ]
            if k not in ignore_keys
        ]

        if "annotations" in item1 and "annotations" in item2:
            if [str(value) for value in item1["annotations"]] != [
                str(value) for value in item2["annotations"]
            ]:
                return False
            item1.pop("annotations")
            item2.pop("annotations")
        elif "annotations" in item1 and "annotations" not in item2:
            if item1["annotations"] != []:
                return False
            item1.pop("annotations")
        elif "annotations" in item2 and "annotations" not in item1:
            if item2["annotations"] != []:
                return False
            item2.pop("annotations")

        for item in (item1, item2):
            if "uuid" in item:
                item["uuid"] = str(item["uuid"])

            if "modified" in item:
                item["modified"] = parse_datetime_(item["modified"])

        return SyncSide._items_are_identical(item1, item2, keys)
