from __future__ import annotations

import copy
import datetime
import hashlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from bubop import logger, parse_datetime
from taskw_ng import TaskWarrior
from taskw_ng.exceptions import TaskwarriorError
from taskw_ng.warrior import TASKRC
from xdg import xdg_config_home

from syncall.change_log import (
    TO_TASKWARRIOR,
    SyncChangeLog,
    diff_stored_items,
    task_name_from_item,
)
from syncall.sync_side import ItemType, SyncSide
from syncall.tw_asana_utils import comment_text_match_keys, normalize_comment_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from syncall.types import TaskwarriorRawItem

tw_duration_key = "syncallduration"
tw_client_key = "client"
tw_notes_key = "notes"
tw_asana_gid_key = "asana_gid"
tw_asana_pending_comments_key = "asana_pending_comments"
tw_asana_comment_state_key = "asana_comment_state"
ASANA_COMMENT_STATE_VERSION = 1

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
        tw_asana_gid_key: {"type": "string", "label": "Asana GID"},
        tw_asana_pending_comments_key: {
            "type": "string",
            "label": "Asana Pending Comments",
        },
        tw_asana_comment_state_key: {
            "type": "string",
            "label": "Asana Comment State",
        },
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
        self.change_log: SyncChangeLog | None = None
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

    def _filter_string(self) -> str:
        filter_parts = [*[f"+{tag}" for tag in self._tags]]
        if self._tw_filter:
            filter_parts.append(self._tw_filter)
        if self._project:
            filter_parts.append(f"pro:{self._project}")
        return f"( {' and '.join(filter_parts)} )"

    def _load_all_items(self):
        """Load all tasks to memory."""
        if not self._reload_items:
            return
        filter_ = self._filter_string()
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

    @staticmethod
    def _annotation_source_entry(annotation: object) -> datetime.datetime | None:
        source_entry = getattr(annotation, "source_entry", None)
        if source_entry is None:
            return None
        return parse_datetime_(source_entry)

    @staticmethod
    def _format_tw_datetime(value: datetime.datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.UTC)
        return value.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")

    def _desired_annotation_entries(
        self,
        annotations: Sequence[object],
    ) -> dict[str, list[str]]:
        desired_by_text: dict[str, list[str]] = {}
        for annotation in annotations:
            source_entry = self._annotation_source_entry(annotation)
            if source_entry is None:
                continue
            desired_by_text.setdefault(str(annotation), []).append(
                self._format_tw_datetime(source_entry),
            )
        return desired_by_text

    def _current_annotation_entries(
        self,
        annotations: Sequence[object],
    ) -> dict[str, list[str]]:
        current_by_text: dict[str, list[str]] = {}
        for annotation in annotations:
            if isinstance(annotation, dict):
                text = str(annotation.get("description") or "")
                entry = annotation.get("entry")
            else:
                text = str(annotation)
                entry = getattr(annotation, "entry", None)
            if entry is None:
                continue
            current_by_text.setdefault(text, []).append(
                self._format_tw_datetime(parse_datetime_(entry)),
            )
        return current_by_text

    def _annotation_timestamps_match(
        self,
        desired_annotations: Sequence[object],
        current_annotations: Sequence[object],
    ) -> bool:
        desired_by_text = self._desired_annotation_entries(desired_annotations)
        current_by_text = self._current_annotation_entries(current_annotations)
        return all(
            sorted(current_by_text.get(text, ())) == sorted(desired_entries)
            for text, desired_entries in desired_by_text.items()
        )

    @staticmethod
    def _annotation_entry(annotation: object) -> datetime.datetime | None:
        """Return the Taskwarrior annotation creation time when one is available."""
        if isinstance(annotation, dict):
            entry = annotation.get("entry")
        else:
            entry = getattr(annotation, "entry", None)
            if entry is None:
                entry = getattr(annotation, "source_entry", None)

        if entry is None:
            return None
        return parse_datetime_(entry)

    @staticmethod
    def _annotation_text(annotation: object) -> str:
        if isinstance(annotation, dict):
            return str(annotation.get("description") or "").strip()
        return str(annotation).strip()

    @classmethod
    def _annotation_text_key(cls, annotation: object) -> str:
        return normalize_comment_text(cls._annotation_text(annotation))

    @classmethod
    def _annotation_match_keys(cls, annotation: object) -> frozenset[str]:
        return comment_text_match_keys(cls._annotation_text(annotation))

    @classmethod
    def _annotation_text_digest(cls, annotation: object) -> str:
        return hashlib.sha256(cls._annotation_text_key(annotation).encode()).hexdigest()[:20]

    @staticmethod
    def _comment_gid(comment: object) -> str | None:
        if isinstance(comment, Mapping):
            gid = comment.get("gid")
        else:
            gid = getattr(comment, "gid", None)
        return str(gid) if gid is not None else None

    @staticmethod
    def _comment_created_at(comment: object) -> datetime.datetime | None:
        if isinstance(comment, Mapping):
            value = comment.get("created_at")
        else:
            value = getattr(comment, "created_at", None)
        if value is None:
            return None
        return parse_datetime_(value)

    @classmethod
    def _parse_comment_state(cls, raw_value: object) -> dict[str, dict[str, str]]:
        if not raw_value:
            return {}
        try:
            parsed = json.loads(str(raw_value))
        except (TypeError, ValueError):
            logger.warning("Ignoring malformed Taskwarrior Asana comment sync state.")
            return {}
        if (
            not isinstance(parsed, dict)
            or parsed.get("version") != ASANA_COMMENT_STATE_VERSION
        ):
            return {}
        bindings = parsed.get("bindings")
        if not isinstance(bindings, dict):
            return {}

        valid: dict[str, dict[str, str]] = {}
        for gid, raw_binding in bindings.items():
            if not isinstance(raw_binding, dict):
                continue
            entry = raw_binding.get("e")
            text_digest = raw_binding.get("h")
            if entry is None:
                continue
            valid[str(gid)] = {
                "e": str(entry),
                "h": str(text_digest or ""),
            }
        return valid

    @staticmethod
    def _serialize_comment_state(bindings: Mapping[str, Mapping[str, str]]) -> str:
        return json.dumps(
            {
                "version": ASANA_COMMENT_STATE_VERSION,
                "bindings": bindings,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _persist_comment_reconciliation(
        self,
        item_id: str,
        raw_task: dict[str, Any],
        annotations: Sequence[object],
        bindings: Mapping[str, Mapping[str, str]],
    ) -> bool:
        serialized = self._serialize_comment_state(bindings)
        annotations_list = list(annotations)
        old_annotations = list(raw_task.get("annotations", ()))
        old_state = raw_task.get(tw_asana_comment_state_key)
        if old_state == serialized and old_annotations == annotations_list:
            return False

        raw_task["annotations"] = annotations_list
        raw_task[tw_asana_comment_state_key] = serialized
        raw_task.pop("id", None)
        raw_task.pop("urgency", None)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", encoding="utf-8"
            ) as handle:
                json.dump(raw_task, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                self._tw._execute("import", handle.name)
        except Exception as exc:
            self._log_annotation_reconciliation(
                item_id,
                raw_task,
                old_annotations,
                old_state,
                annotations_list,
                serialized,
                error=exc,
            )
            raise

        cached = self._items_cache.get(str(item_id))
        if cached is not None:
            cached["annotations"] = annotations_list  # type: ignore[literal-required]
            cached[tw_asana_comment_state_key] = serialized  # type: ignore[literal-required]
        self._reload_items = True
        self._log_annotation_reconciliation(
            item_id,
            raw_task,
            old_annotations,
            old_state,
            annotations_list,
            serialized,
        )
        return True

    def _log_annotation_reconciliation(
        self,
        item_id: str,
        raw_task: dict[str, Any],
        old_annotations: Sequence[object],
        old_state: object,
        new_annotations: Sequence[object],
        new_state: str,
        *,
        error: Exception | None = None,
    ) -> None:
        """Record annotation or comment-identity edits made on the Taskwarrior task."""
        change_log = getattr(self, "change_log", None)
        if change_log is None:
            return

        before = {
            "description": raw_task.get("description"),
            "client": raw_task.get("client"),
            "annotations": old_annotations,
            "asana_comment_state": old_state,
        }
        after = {
            "description": raw_task.get("description"),
            "client": raw_task.get("client"),
            "annotations": new_annotations,
            "asana_comment_state": new_state,
        }
        fields, texts = diff_stored_items(before, after, asana=False)
        annotation_changes = tuple(texts)
        state_changes = tuple(field for field in fields if field.name == "asana_comment_state")
        if error is not None:
            operation = "updated"
            note = "Taskwarrior annotation reconciliation failed. Asana was not changed by this step."
            logged_fields = ()
            logged_texts = ()
        elif annotation_changes:
            operation = "updated"
            note = (
                "Asana comment projections on this Taskwarrior task changed. "
                "Existing Asana comments were not edited."
            )
            logged_fields = state_changes
            logged_texts = annotation_changes
        elif state_changes:
            operation = "comment identity repaired"
            note = (
                "Rebound comment identities on this Taskwarrior task. "
                "Annotation text was not changed. Asana was not changed."
            )
            logged_fields = state_changes
            logged_texts = ()
        else:
            return

        change_log.record(
            direction=TO_TASKWARRIOR,
            operation=operation,
            result="failed" if error is not None else "succeeded",
            tw_uuid=str(item_id),
            asana_gid=str(raw_task.get(tw_asana_gid_key) or "") or None,
            task_name=task_name_from_item(raw_task),
            fields=logged_fields,
            texts=logged_texts,
            note=note,
            error=None if error is None else f"{type(error).__name__}: {error}",
        )

    @classmethod
    def _find_annotation_by_entry(
        cls,
        annotations: Sequence[object],
        used: set[int],
        entry: str,
    ) -> int | None:
        for index, annotation in enumerate(annotations):
            if index in used:
                continue
            annotation_entry = cls._annotation_entry(annotation)
            if annotation_entry is None:
                continue
            if cls._format_tw_datetime(annotation_entry) == entry:
                return index
        return None

    @classmethod
    def _find_annotation_by_text(
        cls,
        annotations: Sequence[object],
        used: set[int],
        match_keys: frozenset[str],
        *,
        target_entry: datetime.datetime | None = None,
    ) -> int | None:
        matches = [
            index
            for index, annotation in enumerate(annotations)
            if index not in used and bool(match_keys & cls._annotation_match_keys(annotation))
        ]
        if not matches or target_entry is None:
            return matches[0] if matches else None

        def distance(index: int) -> float:
            annotation_entry = cls._annotation_entry(annotations[index])
            if annotation_entry is None:
                return float("inf")
            if annotation_entry.tzinfo is None:
                annotation_entry = annotation_entry.replace(tzinfo=datetime.UTC)
            target = target_entry
            if target.tzinfo is None:
                target = target.replace(tzinfo=datetime.UTC)
            return abs((annotation_entry - target).total_seconds())

        return min(matches, key=distance)

    def _project_remote_comment(
        self,
        *,
        item_id: str,
        gid: str,
        remote: object,
        annotations: list[object],
        used_annotations: set[int],
        annotation_index: int | None,
        fallback_entry: str | None = None,
    ) -> dict[str, str] | None:
        """Project one live Asana comment into its Taskwarrior annotation."""
        remote_entry = self._comment_created_at(remote)
        if annotation_index is None:
            if remote_entry is None:
                logger.warning(
                    f"Cannot project Asana comment {gid} onto Taskwarrior task {item_id}: "
                    "remote comment has no creation timestamp.",
                )
                if fallback_entry is None:
                    return None
                return {
                    "e": fallback_entry,
                    "h": self._annotation_text_digest(remote),
                }

            annotations.append(
                {
                    "entry": self._format_tw_datetime(remote_entry),
                    "description": self._annotation_text(remote),
                },
            )
            annotation_index = len(annotations) - 1
        else:
            existing = annotations[annotation_index]
            if isinstance(existing, Mapping):
                annotations[annotation_index] = {
                    **existing,
                    "description": self._annotation_text(remote),
                }

        used_annotations.add(annotation_index)
        annotation_entry = self._annotation_entry(annotations[annotation_index])
        if annotation_entry is None:
            if fallback_entry is None:
                return None
            entry_value = fallback_entry
        else:
            entry_value = self._format_tw_datetime(annotation_entry)

        return {
            "e": entry_value,
            "h": self._annotation_text_digest(remote),
        }

    def _reconcile_bound_comments(
        self,
        *,
        item_id: str,
        annotations: list[object],
        bindings: Mapping[str, Mapping[str, str]],
        remote_by_gid: Mapping[str, object],
        used_annotations: set[int],
        used_remote: set[str],
        rebuilt: dict[str, dict[str, str]],
        delete_indices: set[int],
    ) -> None:
        """Reconcile comments that already have durable Asana GID bindings."""
        for gid, binding in bindings.items():
            remote = remote_by_gid.get(gid)
            entry = binding["e"]
            annotation_index = self._find_annotation_by_entry(
                annotations,
                used_annotations,
                entry,
            )

            if remote is None:
                if annotation_index is not None:
                    used_annotations.add(annotation_index)
                    delete_indices.add(annotation_index)
                rebuilt[gid] = {
                    "e": entry,
                    "h": binding.get("h", ""),
                }
                continue

            projected = self._project_remote_comment(
                item_id=item_id,
                gid=gid,
                remote=remote,
                annotations=annotations,
                used_annotations=used_annotations,
                annotation_index=annotation_index,
                fallback_entry=entry,
            )
            if projected is not None:
                rebuilt[gid] = projected
            used_remote.add(gid)

    def _match_unbound_remote_comment(
        self,
        annotations: Sequence[object],
        used_annotations: set[int],
        remote: object,
    ) -> int | None:
        """Find an existing local annotation corresponding to an unbound Asana comment."""
        remote_entry = self._comment_created_at(remote)
        if remote_entry is not None:
            annotation_index = self._find_annotation_by_entry(
                annotations,
                used_annotations,
                self._format_tw_datetime(remote_entry),
            )
            if annotation_index is not None:
                return annotation_index

        return self._find_annotation_by_text(
            annotations,
            used_annotations,
            self._annotation_match_keys(remote),
            target_entry=remote_entry,
        )

    def _reconcile_unbound_remote_comments(
        self,
        *,
        item_id: str,
        annotations: list[object],
        remote_by_gid: Mapping[str, object],
        used_annotations: set[int],
        used_remote: set[str],
        rebuilt: dict[str, dict[str, str]],
    ) -> None:
        """Recover or create local annotations for live Asana comments without bindings."""
        for gid, remote in remote_by_gid.items():
            if gid in used_remote:
                continue

            annotation_index = self._match_unbound_remote_comment(
                annotations,
                used_annotations,
                remote,
            )
            projected = self._project_remote_comment(
                item_id=item_id,
                gid=gid,
                remote=remote,
                annotations=annotations,
                used_annotations=used_annotations,
                annotation_index=annotation_index,
            )
            if projected is None:
                continue

            rebuilt[gid] = projected
            used_remote.add(gid)

    @staticmethod
    def _remove_deleted_comment_annotations(
        annotations: list[object],
        used_annotations: set[int],
        delete_indices: set[int],
    ) -> tuple[list[object], set[int]]:
        """Drop projections deleted in Asana and remap the surviving used indices."""
        if not delete_indices:
            return annotations, used_annotations

        remaining = [
            annotation
            for index, annotation in enumerate(annotations)
            if index not in delete_indices
        ]
        remapped_used = {
            index - sum(deleted < index for deleted in delete_indices)
            for index in used_annotations
            if index not in delete_indices
        }
        return remaining, remapped_used

    def _unsynced_local_annotations(
        self,
        item_id: str,
        annotations: Sequence[object],
        used_annotations: set[int],
    ) -> list[str]:
        """Return local-only annotations that are eligible to append to Asana."""
        unsynced: list[str] = []
        for index, annotation in enumerate(annotations):
            if index in used_annotations:
                continue
            if self._annotation_entry(annotation) is None:
                logger.warning(
                    f"Skipping outbound comment for Taskwarrior task {item_id}: "
                    "annotation has no stable creation timestamp.",
                )
                continue
            text = self._annotation_text(annotation)
            if text:
                unsynced.append(text)
        return unsynced

    def reconcile_asana_comment_state(
        self,
        item_id: str,
        current_item: Mapping[str, Any],
        remote_comments: Sequence[object],
    ) -> list[str]:
        """Converge Asana-backed annotations and return genuinely local-only annotations."""
        raw_task = self._load_exported_task(item_id)
        if raw_task is None:
            return []

        annotations = list(raw_task.get("annotations", current_item.get("annotations", ())))
        bindings = self._parse_comment_state(raw_task.get(tw_asana_comment_state_key))
        remote_by_gid = {
            gid: comment
            for comment in remote_comments
            if (gid := self._comment_gid(comment)) is not None
        }
        used_annotations: set[int] = set()
        used_remote: set[str] = set()
        rebuilt: dict[str, dict[str, str]] = {}
        delete_indices: set[int] = set()

        self._reconcile_bound_comments(
            item_id=item_id,
            annotations=annotations,
            bindings=bindings,
            remote_by_gid=remote_by_gid,
            used_annotations=used_annotations,
            used_remote=used_remote,
            rebuilt=rebuilt,
            delete_indices=delete_indices,
        )
        self._reconcile_unbound_remote_comments(
            item_id=item_id,
            annotations=annotations,
            remote_by_gid=remote_by_gid,
            used_annotations=used_annotations,
            used_remote=used_remote,
            rebuilt=rebuilt,
        )
        annotations, used_annotations = self._remove_deleted_comment_annotations(
            annotations,
            used_annotations,
            delete_indices,
        )
        self._persist_comment_reconciliation(item_id, raw_task, annotations, rebuilt)
        return self._unsynced_local_annotations(item_id, annotations, used_annotations)

    def reconcile_annotation_timestamps(
        self,
        item_id: str,
        desired_annotations: Sequence[object],
        *,
        current_annotations: Sequence[object] | None = None,
    ) -> int:
        """Repair source-backed annotation dates only when the current dates differ."""
        desired_with_dates = [
            annotation
            for annotation in desired_annotations
            if self._annotation_source_entry(annotation) is not None
        ]
        if not desired_with_dates:
            return 0

        if current_annotations is not None and self._annotation_timestamps_match(
            desired_with_dates,
            current_annotations,
        ):
            return 0

        return self._repair_annotation_timestamps(item_id, desired_with_dates)

    def _load_exported_task(self, item_id: str) -> dict[str, Any] | None:
        raw_export = self._tw._get_json(item_id, "export")
        if isinstance(raw_export, list):
            return raw_export[0] if raw_export else None
        return raw_export

    def _group_desired_annotations(
        self,
        annotations: Sequence[object],
    ) -> dict[str, list[object]]:
        grouped: dict[str, list[object]] = {}
        for annotation in annotations:
            grouped.setdefault(str(annotation), []).append(annotation)
        return grouped

    @staticmethod
    def _group_current_annotations(
        annotations: Sequence[object],
    ) -> dict[str, list[tuple[int, dict[str, Any]]]]:
        grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, annotation in enumerate(annotations):
            if not isinstance(annotation, dict):
                continue
            text = str(annotation.get("description") or "")
            grouped.setdefault(text, []).append((index, annotation))
        return grouped

    def _repair_annotation_group(
        self,
        *,
        item_id: str,
        text: str,
        desired_group: list[object],
        current_group: list[tuple[int, dict[str, Any]]],
        current_annotations: list[object],
    ) -> int:
        if len(current_group) != len(desired_group):
            logger.warning(
                f"Skipping ambiguous annotation timestamp repair for Taskwarrior task "
                f"{item_id}: {text!r} occurs {len(current_group)} time(s) locally and "
                f"{len(desired_group)} time(s) in Asana.",
            )
            return 0

        desired_group.sort(
            key=lambda annotation: (
                self._annotation_source_entry(annotation)
                or datetime.datetime.min.replace(tzinfo=datetime.UTC)
            ),
        )
        current_group.sort(
            key=lambda pair: (
                parse_datetime_(pair[1]["entry"])
                if pair[1].get("entry")
                else datetime.datetime.min.replace(tzinfo=datetime.UTC)
            ),
        )

        repaired = 0
        for (index, current), desired_annotation in zip(
            current_group,
            desired_group,
            strict=True,
        ):
            source_entry = self._annotation_source_entry(desired_annotation)
            if source_entry is None:
                continue
            expected_entry = self._format_tw_datetime(source_entry)
            if current.get("entry") == expected_entry:
                continue
            current_annotations[index] = {
                **current,
                "entry": expected_entry,
            }
            repaired += 1
        return repaired

    def _repair_annotation_timestamps(
        self,
        item_id: str,
        desired_annotations: Sequence[object],
    ) -> int:
        raw_task = self._load_exported_task(item_id)
        if raw_task is None:
            return 0

        current_annotations = list(raw_task.get("annotations", ()))
        if not current_annotations:
            return 0

        desired_by_text = self._group_desired_annotations(desired_annotations)
        current_by_text = self._group_current_annotations(current_annotations)
        repaired = sum(
            self._repair_annotation_group(
                item_id=item_id,
                text=text,
                desired_group=desired_group,
                current_group=current_by_text.get(text, []),
                current_annotations=current_annotations,
            )
            for text, desired_group in desired_by_text.items()
        )
        if repaired == 0:
            return 0

        raw_task["annotations"] = current_annotations
        raw_task.pop("id", None)
        raw_task.pop("urgency", None)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as handle:
            json.dump(raw_task, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            self._tw._execute("import", handle.name)

        self._reload_items = True
        logger.debug(
            f"Repaired {repaired} annotation timestamp(s) on Taskwarrior task {item_id}.",
        )
        return repaired

    def update_item(self, item_id: str, **changes):
        changes.pop("id", False)
        # Existing-task comments are reconciled independently from whole-task scalar sync.
        changes.pop("annotations", None)
        t = self._tw.get_task(uuid=UUID(item_id))[-1]

        unwanted_keys = ["imask", "recur", "rtype", "parent", "urgency"]
        for i in unwanted_keys:
            t.pop(i, False)

        d = dict(t)
        d.update(changes)
        self._tw.task_update(d)
        self._reload_items = True

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

    def record_asana_gid(self, item_id: str, asana_gid: str) -> bool:
        """Persist Asana identity and a crash-recovery marker immediately."""
        try:
            self._tw._execute(
                str(item_id),
                "modify",
                f"{tw_asana_gid_key}:{asana_gid}",
                f"{tw_asana_pending_comments_key}:1",
            )
        except (OSError, TaskwarriorError) as exc:
            logger.opt(exception=True).warning(
                f"Could not persist Asana identity on Taskwarrior task {item_id}.",
            )
            self._log_identity(
                item_id,
                asana_gid,
                operation="identity stored",
                error=exc,
                note="Could not store the new Asana task id on the Taskwarrior task.",
            )
            return False

        cached = self._items_cache.get(str(item_id))
        if cached is not None:
            cached[tw_asana_gid_key] = str(asana_gid)  # type: ignore[literal-required]
            cached[tw_asana_pending_comments_key] = "1"  # type: ignore[literal-required]
        self._reload_items = True
        self._log_identity(
            item_id,
            asana_gid,
            operation="identity stored",
            note=(
                "Stored the new Asana task id on the Taskwarrior task after creating it. "
                "This step did not change Asana task content."
            ),
        )
        return True

    def clear_pending_asana_comments(self, item_id: str) -> None:
        """Clear the recovery marker after all outbound comments are confirmed remote."""
        try:
            self._tw._execute(
                str(item_id),
                "modify",
                f"{tw_asana_pending_comments_key}:",
            )
        except Exception as exc:
            self._log_identity(
                item_id,
                None,
                operation="recovery marker cleared",
                error=exc,
                note="Could not clear the Taskwarrior crash-recovery marker.",
            )
            raise
        cached = self._items_cache.get(str(item_id))
        if cached is not None:
            cached.pop(tw_asana_pending_comments_key, None)
        self._reload_items = True
        self._log_identity(
            item_id,
            str(cached.get(tw_asana_gid_key))
            if cached and cached.get(tw_asana_gid_key)
            else None,
            operation="recovery marker cleared",
            note=(
                "Cleared the crash-recovery marker after outbound comments were confirmed. "
                "This step did not change Asana task content."
            ),
        )

    def backfill_asana_gids(self, mapping: Mapping[str, str]) -> int:
        """Persist Asana task identity inside Taskwarrior so mappings are recoverable."""
        if not mapping:
            return 0

        raw_tasks = self._tw._get_json(self._filter_string(), "export")
        if isinstance(raw_tasks, dict):
            raw_tasks = [raw_tasks]

        changed = []
        for raw_task in raw_tasks:
            task_uuid = str(raw_task.get("uuid") or "")
            asana_gid = mapping.get(task_uuid)
            previous_gid = str(raw_task.get(tw_asana_gid_key) or "")
            if asana_gid is None or previous_gid == str(asana_gid):
                continue
            raw_task[tw_asana_gid_key] = str(asana_gid)
            raw_task.pop("id", None)
            raw_task.pop("urgency", None)
            changed.append((previous_gid, raw_task))

        if not changed:
            return 0

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", encoding="utf-8"
            ) as handle:
                for _previous_gid, raw_task in changed:
                    json.dump(raw_task, handle, ensure_ascii=False)
                    handle.write("\n")
                handle.flush()
                self._tw._execute("import", handle.name)
        except Exception as exc:
            self._log_identity(
                "",
                None,
                operation="identity stored",
                error=exc,
                note="Could not backfill Asana task ids onto Taskwarrior tasks.",
            )
            raise

        self._reload_items = True
        for previous_gid, raw_task in changed:
            self._log_identity(
                str(raw_task.get("uuid") or ""),
                str(raw_task.get(tw_asana_gid_key) or ""),
                operation="identity stored",
                task_name=task_name_from_item(raw_task),
                note=(
                    f"Asana task id stored on this Taskwarrior task "
                    f"(previous value: {previous_gid or '(empty)'}). "
                    "Asana task content was not changed."
                ),
            )
        return len(changed)

    def _log_identity(
        self,
        item_id: str,
        asana_gid: str | None,
        *,
        operation: str,
        note: str,
        error: Exception | None = None,
        task_name: str | None = None,
    ) -> None:
        """Record a Taskwarrior identity or recovery-marker write."""
        change_log = getattr(self, "change_log", None)
        if change_log is None:
            return
        change_log.record(
            direction=TO_TASKWARRIOR,
            operation=operation,
            result="failed" if error is not None else "succeeded",
            tw_uuid=item_id or None,
            asana_gid=asana_gid,
            task_name=task_name,
            note=note,
            error=None if error is None else f"{type(error).__name__}: {error}",
        )

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
