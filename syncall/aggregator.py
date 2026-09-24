from __future__ import annotations

from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from item_synchronizer.types import ID, ConverterFn, Item

    from syncall.sync_side import SyncSide

from contextlib import nullcontext
from dataclasses import is_dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from bidict import bidict  # pyright: ignore[reportPrivateImportUsage]
from bubop import PrefsManager, logger, parse_datetime, pickle_dump, pickle_load
from bubop.time import is_same_datetime
from item_synchronizer import Synchronizer
from item_synchronizer.helpers import SideChanges
from item_synchronizer.resolution_strategy import AlwaysSecondRS, ResolutionStrategy
from rich.console import Console
from yaml import YAMLError

from syncall.app_utils import app_name
from syncall.change_log import (
    TO_ASANA,
    TO_TASKWARRIOR,
    SyncChangeLog,
    created_item_changes,
    diff_stored_items,
    task_name_from_item,
)
from syncall.progress import make_progress
from syncall.side_helper import SideHelper


def _format_sync_failures(errors: list[BaseException]) -> str:
    """Return the short underlying messages for failed sync writes."""
    parts: list[str] = []
    for exc in errors:
        stderr = getattr(exc, "stderr", None)
        if isinstance(stderr, bytes):
            text = stderr.decode(errors="replace")
        elif stderr:
            text = str(stderr)
        else:
            text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        text = text.strip().strip('"')
        if text:
            parts.append(text)
    return " ".join(parts)


class Aggregator:
    """Aggregator class that manages the synchronization between two arbitrary sides.

    Having an aggregator is handy for managing push/pull/sync directives in a
    consistent manner.
    """

    def __init__(
        self,
        *,
        side_A: SyncSide,
        side_B: SyncSide,
        converter_B_to_A: ConverterFn,
        converter_A_to_B: ConverterFn,
        resolution_strategy: ResolutionStrategy | None = None,
        config_fname: str | None = None,
        ignore_keys: tuple[Sequence[str], Sequence[str]] = (),
        catch_exceptions: bool = True,
        change_log: SyncChangeLog | None = None,
    ):
        # Preferences manager
        # Sample config path: ~/.config/syncall/taskwarrior_gcal_sync.yaml
        #                     ~/.config/syncall/taskwarrior_notion_sync.yaml
        #
        # The stem of the filename can be overridden by the user if they provide `config_fname`.
        #
        # Serdes dirs are shared across multiple different syncrhonizers
        # Sample serdes dirs: ~/.config/syncall/serdes/gcal/
        #                     ~/.config/syncall/serdes/tw/
        if config_fname is None:
            config_fname = f"{side_B.name}_{side_A.name}_sync".lower()
        else:
            logger.debug(f"Using a custom configuration file ... -> {config_fname}")

        if resolution_strategy is None:
            resolution_strategy = AlwaysSecondRS()

        self.prefs_manager = PrefsManager(app_name=app_name(), config_fname=config_fname)

        # Own config
        self.config: dict[str, Any] = {}

        self._side_A: SyncSide = side_A
        self._side_B: SyncSide = side_B

        # Initialize helpers - one for each side ----------------------------------------------
        self._helper_A = SideHelper.from_side(self._side_A)
        self._helper_B = SideHelper.from_side(self._side_B)
        self._helper_A.other = self._helper_B
        self._helper_B.other = self._helper_A

        # ignore keys
        if ignore_keys:
            self._helper_A.ignore_keys = ignore_keys[0]
            self._helper_B.ignore_keys = ignore_keys[1]

        # serdes directories for storing cached versions of items for each side ---------------
        self.serdes_dirs = self.prefs_manager.config_directory / "serdes"
        serdes_A = self.serdes_dirs / self._side_A.name.lower()
        serdes_B = self.serdes_dirs / self._side_B.name.lower()
        serdes_A.mkdir(exist_ok=True, parents=True)
        serdes_B.mkdir(exist_ok=True)

        self.config[f"{self._helper_A}_serdes"] = serdes_A
        self.config[f"{self._helper_B}_serdes"] = serdes_B

        # Correspondences between the two sides -----------------------------------------------
        # For finding the matches between IDs of the two sides
        # e.g., for Taskwarrior <-> GCal: tw_gcal_ids
        correspondences_prefs_key = f"{self._side_B.name}_{self._side_A.name}_ids"
        if correspondences_prefs_key not in self.prefs_manager:
            self.prefs_manager[correspondences_prefs_key] = bidict()
        self._B_to_A_map: bidict = self.prefs_manager[correspondences_prefs_key]

        # resolution strategy to resolve conflicts
        self._resolution_strategy = resolution_strategy
        self._plain_converter_B_to_A = converter_B_to_A
        converter_to_A = (
            self._convert_existing_tw_to_asana
            if side_A.name == "Asana" and side_B.name == "Tw"
            else converter_B_to_A
        )

        # item synchronizer -------------------------------------------------------------------
        def side_B_fn(fn):
            wrapped = partial(fn, helper=self._helper_B)
            wrapped.__doc__ = f"{self._helper_B} {fn.__doc__}"
            return wrapped

        def side_A_fn(fn):
            wrapped = partial(fn, helper=self._helper_A)
            wrapped.__doc__ = f"{self._helper_A} {fn.__doc__}"
            return wrapped

        self._synchronizer = Synchronizer(
            A_to_B=self._B_to_A_map.inverse,
            inserter_to_A=side_A_fn(self.inserter_to),
            inserter_to_B=side_B_fn(self.inserter_to),
            updater_to_A=side_A_fn(self.updater_to),
            updater_to_B=side_B_fn(self.updater_to),
            deleter_to_A=side_A_fn(self.deleter_to),
            deleter_to_B=side_B_fn(self.deleter_to),
            converter_to_A=converter_to_A,
            converter_to_B=converter_A_to_B,
            item_getter_A=side_A_fn(self.item_getter_for),
            item_getter_B=side_B_fn(self.item_getter_for),
            resolution_strategy=self._resolution_strategy,
            catch_exceptions=catch_exceptions,
            side_names=(side_A.fullname, side_B.fullname),
        )

        self.change_log = change_log
        self._items_A: dict[ID, Item] = {}
        self._items_B: dict[ID, Item] = {}
        self._operation_progress = None
        self._operation_progress_task = None
        self._operation_failed = False
        self._operation_errors: list[BaseException] = []
        self._written_serdes: set[tuple[str, ID]] = set()
        self._live_asana_comments: dict[ID, Sequence[object]] = {}
        self.cleaned_up = False

    def __enter__(self) -> Self:
        """Enter context manager."""
        self.start()
        return self

    def __exit__(self, *_) -> None:
        """Exit context manager."""
        self.finish()

    def detect_changes(self, helper: SideHelper, items: dict[ID, Item]) -> SideChanges:
        """Detect changes between the two sides.

        Given a fresh list of items from the SyncSide, determine which of them are new,
        modified, or have been deleted since the last run.
        """
        serdes_dir, _ = self._get_serdes_dirs(helper)
        logger.info(f"Detecting changes from {helper}...")
        item_ids = set(items.keys())
        # New items exist in the sync side but don't yet exist in my IDs correspndences.
        new = {
            item_id for item_id in item_ids if item_id not in self._get_ids_map(helper=helper)
        }
        # An item missing from the filtered result is not necessarily deleted. It may simply
        # have fallen out of scope (for example a removed Taskwarrior tag or an Asana task that
        # is no longer assigned to/followed by the user). Verify each missing registered item
        # directly before propagating a deletion.
        missing_registered_ids = {
            registered_id
            for registered_id in self._get_ids_map(helper=helper)
            if registered_id not in item_ids.difference(new)
        }
        side, _ = self._get_side_instances(helper)
        deleted = set()
        modified = set()
        potentially_modified_ids = item_ids.difference(new)

        work_total = len(missing_registered_ids) + len(potentially_modified_ids)
        progress = make_progress(console=Console(), unit="items")
        progress_task = None
        if work_total:
            progress.start()
            progress_task = progress.add_task(
                f"Checking {helper} changes",
                total=work_total,
            )

        try:
            for registered_id in missing_registered_ids:
                if side.get_item(registered_id) is None:
                    deleted.add(registered_id)
                else:
                    logger.debug(
                        f"[{helper}] Item {registered_id} still exists but is outside the "
                        "current sync scope; not treating it as deleted.",
                    )
                if progress_task is not None:
                    progress.advance(progress_task)

            potentially_modified_ids = potentially_modified_ids.difference(deleted)
            for item_id in potentially_modified_ids:
                item = items[item_id]
                cached_path = serdes_dir / item_id
                if not cached_path.is_file():
                    # Identity may be recoverable even if local sync snapshots were deleted.
                    # Baseline the live item rather than guessing a sync direction and writing
                    # remote data from incomplete local history.
                    logger.warning(
                        f"[{helper}] Missing sync snapshot for mapped item {item_id}; "
                        "baselining current state without propagating a change.",
                    )
                    pickle_dump(item, cached_path)
                else:
                    cached_item = pickle_load(cached_path)
                    if self._item_has_update(
                        prev_item=cached_item,
                        new_item=item,
                        helper=helper,
                    ):
                        modified.add(item_id)
                if progress_task is not None:
                    progress.advance(progress_task)
        finally:
            if progress_task is not None:
                progress.stop()

        side_changes = SideChanges(new=new, modified=modified, deleted=deleted)
        logger.debug(f"\n\n{side_changes}")
        Console().print(
            f"[bold]{helper} changes:[/bold] "
            f"{len(new):,} new, {len(modified):,} modified, {len(deleted):,} deleted",
        )

        return side_changes

    def sync(self) -> None:
        """Entrypoint method."""
        console = Console()

        self._items_A = {
            str(item[self._helper_A.id_key]): item for item in self._side_A.get_all_items()
        }
        with console.status("[bold]Loading Taskwarrior snapshot...[/bold]", spinner="dots"):
            self._items_B = {
                str(item[self._helper_B.id_key]): item for item in self._side_B.get_all_items()
            }
        console.print(
            f"[bold]Found {len(self._items_B):,} Taskwarrior tasks in sync scope[/bold]"
        )

        self._sync_comment_histories()
        with console.status(
            "[bold]Reloading Taskwarrior after comment reconciliation...[/bold]",
            spinner="dots",
        ):
            self._items_B = {
                str(item[self._helper_B.id_key]): item for item in self._side_B.get_all_items()
            }

        changes_A = self.detect_changes(self._helper_A, self._items_A)
        changes_B = self.detect_changes(self._helper_B, self._items_B)

        side_A_serdes_dir, side_B_serdes_dir = self._get_serdes_dirs(self._helper_A)
        cache_items = [
            *(
                (self._helper_B, item_id, self._items_B[item_id], side_B_serdes_dir)
                for item_id in changes_B.new.union(changes_B.modified)
            ),
            *(
                (self._helper_A, item_id, self._items_A[item_id], side_A_serdes_dir)
                for item_id in changes_A.new.union(changes_A.modified)
            ),
        ]

        total_operations = self._count_sync_operations(changes_A, changes_B)
        if total_operations == 0:
            console.print("[bold green]Already in sync[/bold green]")
            return

        self._operation_failed = False
        self._operation_errors = []
        self._written_serdes = set()
        progress = make_progress(console=console, unit="ops")
        with progress:
            self._operation_progress = progress
            self._operation_progress_task = progress.add_task(
                "Applying sync changes",
                total=total_operations,
            )
            try:
                self._synchronizer.sync(changes_A=changes_A, changes_B=changes_B)
            finally:
                self._operation_progress = None
                self._operation_progress_task = None
                self.flush_correspondences()

        if self._operation_failed:
            detail = _format_sync_failures(self._operation_errors)
            raise RuntimeError(
                "One or more sync writes failed; sync state was not committed so the "
                "next run can retry safely." + (f" {detail}" if detail else ""),
            )

        if cache_items:
            progress = make_progress(console=console, unit="items")
            with progress:
                progress_task = progress.add_task("Saving sync state", total=len(cache_items))
                for helper, item_id, item, serdes_dir in cache_items:
                    if (helper.name, item_id) not in self._written_serdes:
                        pickle_dump(item, serdes_dir / item_id)
                    progress.advance(progress_task)

        self._remove_serdes_files(helper=self._helper_B, ids=changes_B.deleted)
        self._remove_serdes_files(helper=self._helper_A, ids=changes_A.deleted)

    def _comment_sync_functions(self):
        """Return optional comment-sync capabilities, or None for unrelated sync sides."""
        get_comments_live = getattr(self._side_A, "get_comments_live", None)
        ensure_comments = getattr(self._side_A, "ensure_comments", None)
        reconcile = getattr(self._side_B, "reconcile_asana_comment_state", None)
        if not all(callable(fn) for fn in (get_comments_live, ensure_comments, reconcile)):
            return None
        return get_comments_live, ensure_comments, reconcile

    def _store_live_comments(self, asana_id: str, asana_item: Item, live_comments) -> None:
        """Keep the current Asana snapshot aligned with freshly fetched comment history."""
        self._live_asana_comments[asana_id] = live_comments
        if hasattr(asana_item, "comments"):
            asana_item.comments = tuple(live_comments)
        elif isinstance(asana_item, dict):
            asana_item["comments"] = tuple(live_comments)

    def _sync_one_comment_history(
        self,
        tw_id: str,
        asana_id: str,
        *,
        get_comments_live,
        ensure_comments,
        reconcile,
    ) -> None:
        """Converge one mapped task's comment history."""
        asana_item = self._items_A.get(asana_id)
        current_tw = self._items_B.get(tw_id)
        if asana_item is None or current_tw is None:
            return

        live_comments = get_comments_live(asana_id)
        self._store_live_comments(asana_id, asana_item, live_comments)
        task_name = task_name_from_item(asana_item) or task_name_from_item(current_tw)
        change_log = getattr(self, "change_log", None)
        comment_context = (
            change_log.task_context(
                tw_uuid=tw_id,
                asana_gid=asana_id,
                task_name=task_name,
            )
            if change_log is not None
            else nullcontext()
        )

        with comment_context:
            self._reconcile_one_comment_history(
                tw_id,
                asana_id,
                asana_item,
                current_tw,
                live_comments,
                get_comments_live=get_comments_live,
                ensure_comments=ensure_comments,
                reconcile=reconcile,
            )

    def _reconcile_one_comment_history(
        self,
        tw_id: str,
        asana_id: str,
        asana_item: Item,
        current_tw: Item,
        live_comments,
        *,
        get_comments_live,
        ensure_comments,
        reconcile,
    ) -> None:
        outbound = reconcile(tw_id, current_tw, live_comments)
        if not outbound:
            return

        ensure_comments(asana_id, outbound)
        live_comments = get_comments_live(asana_id)
        self._store_live_comments(asana_id, asana_item, live_comments)

        refreshed_tw = self._side_B.get_item(tw_id, use_cached=False)
        if refreshed_tw is None:
            raise RuntimeError(
                f"Taskwarrior task {tw_id} disappeared while checkpointing comments.",
            )
        remaining = reconcile(tw_id, refreshed_tw, live_comments)
        if remaining:
            raise RuntimeError(
                f"Taskwarrior task {tw_id} still has unsynchronized comments after "
                "Asana append/read-back; refusing to continue.",
            )

    def _sync_comment_histories(self) -> None:
        """Converge mapped Asana/TW comment history before generic task synchronization."""
        functions = self._comment_sync_functions()
        if functions is None:
            return

        get_comments_live, ensure_comments, reconcile = functions
        self._live_asana_comments = {}
        pairs = [
            (str(tw_ref), str(asana_ref)) for tw_ref, asana_ref in self._B_to_A_map.items()
        ]
        if not pairs:
            return

        progress = make_progress(console=Console(), unit="tasks")
        with progress:
            progress_task = progress.add_task("Reconciling comments", total=len(pairs))
            for tw_ref, asana_ref in pairs:
                try:
                    self._sync_one_comment_history(
                        tw_ref,
                        asana_ref,
                        get_comments_live=get_comments_live,
                        ensure_comments=ensure_comments,
                        reconcile=reconcile,
                    )
                finally:
                    progress.advance(progress_task)

    def start(self) -> None:
        """Initialize the aggregator."""
        self._side_A.start()
        self._side_B.start()

    def finish(self) -> None:
        """Finalize the aggregator."""
        self._side_A.finish()
        self._side_B.finish()

    def recover_correspondences(self, recovered: dict[str, str]) -> int:
        """Merge independently recoverable B→A identities into the correspondence map."""
        recovered_count = 0
        for id_B, id_A in recovered.items():
            existing_A = self._B_to_A_map.get(id_B)
            existing_B = self._B_to_A_map.inverse.get(id_A)
            if existing_A == id_A:
                continue
            if existing_A is not None or existing_B is not None:
                raise RuntimeError(
                    f"Conflicting recovered correspondence: {id_B!r} -> {id_A!r}.",
                )
            self._B_to_A_map[id_B] = id_A
            recovered_count += 1
        return recovered_count

    def flush_correspondences(self) -> None:
        """Persist correspondence changes immediately rather than waiting for process exit."""
        self.prefs_manager.flush_config(self.prefs_manager.config_file)

    def _count_sync_operations(self, changes_A: SideChanges, changes_B: SideChanges) -> int:
        """Count the number of create/update/delete operations the synchronizer will perform."""
        touched_A = changes_A.modified.union(changes_A.deleted)
        touched_B = changes_B.modified.union(changes_B.deleted)
        mapped_touched_A_in_B = {
            self._B_to_A_map.inverse[item_id]
            for item_id in touched_A
            if item_id in self._B_to_A_map.inverse
        }
        conflicts = touched_B.intersection(mapped_touched_A_in_B)
        touched_operations = len(touched_A) + len(touched_B) - len(conflicts)
        return len(changes_A.new) + len(changes_B.new) + touched_operations

    def _advance_operation_progress(self) -> None:
        if self._operation_progress is not None and self._operation_progress_task is not None:
            self._operation_progress.advance(self._operation_progress_task)

    def _record_operation_failure(
        self,
        helper: SideHelper,
        action: str,
        exc: BaseException,
    ) -> None:
        """Remember a failed write and log the underlying cause at error level.

        item_synchronizer also logs "Operation failed", but only the exception type is
        visible unless verbose mode is on. The Taskwarrior/Asana message is the useful part.
        """
        self._operation_failed = True
        errors = getattr(self, "_operation_errors", None)
        if errors is None:
            errors = []
            self._operation_errors = errors
        errors.append(exc)
        logger.error(f"[{helper}] {action} failed: {_format_sync_failures([exc])}")

    def _convert_existing_tw_to_asana(self, tw_item: Item) -> Item | None:
        """Convert a Taskwarrior task, skipping Asana writes that are not real TW field edits."""
        converted = self._plain_converter_B_to_A(tw_item)
        if converted is None:
            return None
        tw_id = str(tw_item.get("uuid") or "")
        if not tw_id or tw_id not in self._B_to_A_map:
            return converted
        limited = self._limit_asana_update_to_tw_changes(tw_id, converted)
        if limited is None:
            # item_synchronizer skips the updater when the converter returns None, so count
            # this as a finished no-op operation or the progress bar never reaches 100%.
            self._advance_operation_progress()
            return None
        return limited

    def _limit_asana_update_to_tw_changes(self, tw_id: str, converted: Item) -> Item | None:
        """Keep live Asana values for fields Taskwarrior did not actually change."""
        asana_keys = self._asana_keys_from_tw_changes(self._changed_tw_source_fields(tw_id))
        if not asana_keys:
            return None

        live = self._items_A.get(str(self._B_to_A_map[tw_id]))
        if live is None:
            return converted

        limited = self._apply_asana_replacements(
            converted,
            self._live_asana_replacements(live, asana_keys),
        )
        if limited is None or self._asana_update_is_noop(live, limited):
            return None
        return limited

    @staticmethod
    def _asana_keys_from_tw_changes(changed: set[str]) -> set[str]:
        asana_keys: set[str] = set()
        if "description" in changed or "client" in changed:
            asana_keys.add("name")
        if "status" in changed:
            asana_keys.add("completed")
        if "due" in changed:
            asana_keys.update(("due_on", "due_at"))
        return asana_keys

    def _live_asana_replacements(self, live: Item, asana_keys: set[str]) -> dict[str, object]:
        replacements: dict[str, object] = {
            "html_notes": self._asana_field(live, "html_notes") or "<body></body>",
            "comments": self._asana_field(live, "comments") or (),
        }
        if "name" not in asana_keys:
            replacements["name"] = self._asana_field(live, "name")
        if "completed" not in asana_keys:
            replacements["completed"] = self._asana_field(live, "completed")
            replacements["completed_at"] = self._asana_field(live, "completed_at")
        if "due_on" not in asana_keys:
            replacements["due_on"] = self._asana_field(live, "due_on")
            replacements["due_at"] = self._asana_field(live, "due_at")
        return replacements

    @staticmethod
    def _apply_asana_replacements(
        converted: Item, replacements: dict[str, object]
    ) -> Item | None:
        if is_dataclass(converted) and not isinstance(converted, type):
            return replace(converted, **replacements)
        if isinstance(converted, dict):
            return {**converted, **replacements}
        return None

    def _asana_update_is_noop(self, live: Item, limited: Item) -> bool:
        return self._side_A.items_are_identical(
            live,
            limited,
            ignore_keys=(
                "comments",
                "completed_at",
                "created_at",
                "gid",
                "html_notes",
                "modified_at",
            ),
        )

    def _changed_tw_source_fields(self, tw_id: str) -> set[str]:
        current = self._items_B.get(tw_id)
        if current is None:
            return set()
        serdes_dir, _ = self._get_serdes_dirs(self._helper_B)
        snapshot_path = serdes_dir / str(tw_id)
        if not snapshot_path.is_file():
            return set()
        previous = pickle_load(snapshot_path)
        changed: set[str] = set()
        for key in ("description", "client", "status", "due"):
            if not self._tw_source_values_match(
                key,
                self._tw_field(previous, key),
                self._tw_field(current, key),
            ):
                changed.add(key)
        return changed

    @staticmethod
    def _tw_field(item: object, name: str) -> object:
        if item is None:
            return None
        if isinstance(item, dict):
            return item.get(name)
        return getattr(item, name, None)

    @staticmethod
    def _asana_field(item: object, name: str) -> object:
        if item is None:
            return None
        if isinstance(item, dict):
            return item.get(name)
        try:
            return item[name]  # type: ignore[index]
        except (KeyError, AttributeError, TypeError):
            return getattr(item, name, None)

    @staticmethod
    def _tw_source_values_match(key: str, left: object, right: object) -> bool:
        if key == "due":
            return Aggregator._due_values_match(left, right)
        left_value = None if left in (None, "") else left
        right_value = None if right in (None, "") else right
        return left_value == right_value

    @staticmethod
    def _due_values_match(left: object, right: object) -> bool:
        if left in (None, "") and right in (None, ""):
            return True
        if left in (None, "") or right in (None, ""):
            return False
        try:
            left_dt = left if isinstance(left, datetime) else parse_datetime(str(left))
            right_dt = right if isinstance(right, datetime) else parse_datetime(str(right))
        except (TypeError, ValueError):
            return str(left) == str(right)
        return is_same_datetime(left_dt, right_dt, tol=timedelta(minutes=1))

    def _checkpoint_correspondence(
        self,
        source_tw_uuid: str,
        item_created_id: str,
    ) -> bool:
        """Persist the correspondence map, returning whether disk persistence succeeded."""
        existing_asana_id = self._B_to_A_map.get(source_tw_uuid)
        if existing_asana_id not in (None, item_created_id):
            raise RuntimeError(
                f"Taskwarrior task {source_tw_uuid} is already mapped to "
                f"Asana task {existing_asana_id}.",
            )

        self._B_to_A_map[source_tw_uuid] = item_created_id
        try:
            self.flush_correspondences()
        except (OSError, YAMLError):
            logger.opt(exception=True).warning(
                "Could not persist the Asana↔Taskwarrior correspondence map.",
            )
            return False
        return True

    def _checkpoint_source_identity(
        self,
        item: Item,
        helper: SideHelper,
        item_created_id: str,
    ) -> str | None:
        """Checkpoint a new Asana task identity through independent durable channels."""
        source_tw_uuid = getattr(item, "source_tw_uuid", None)
        if source_tw_uuid is None:
            return None

        source_tw_uuid = str(source_tw_uuid)
        _, source_side = self._get_side_instances(helper)
        checkpoint_successes = int(
            self._checkpoint_correspondence(source_tw_uuid, item_created_id),
        )

        record_asana_gid = getattr(source_side, "record_asana_gid", None)
        if callable(record_asana_gid) and record_asana_gid(source_tw_uuid, item_created_id):
            checkpoint_successes += 1

        if checkpoint_successes:
            return source_tw_uuid

        raise RuntimeError(
            "Could not durably checkpoint the new Asana task identity; "
            "refusing to create comments.",
        )

    def _run_post_create_sync(
        self,
        item_side: SyncSide,
        item_created_id: str,
        item: Item,
    ) -> None:
        """Run optional post-create work after identity has been checkpointed."""
        post_create_sync = getattr(item_side, "post_create_sync", None)
        if callable(post_create_sync):
            post_create_sync(item_created_id, item)

    def _clear_pending_comments(
        self,
        source_tw_uuid: str | None,
        helper: SideHelper,
    ) -> None:
        """Clear the optional Taskwarrior crash-recovery marker."""
        if source_tw_uuid is None:
            return

        _, source_side = self._get_side_instances(helper)
        clear_pending = getattr(source_side, "clear_pending_asana_comments", None)
        if callable(clear_pending):
            clear_pending(source_tw_uuid)

    def inserter_to(self, item: Item, helper: SideHelper) -> ID:
        """Insert an item using the given side helper.

        Other side already has the item, and I'm also inserting it at this side.
        """
        item_side, _ = self._get_side_instances(helper)
        serdes_dir, _ = self._get_serdes_dirs(helper)
        logger.debug(
            f"[{helper.other}] Inserting item [{self._summary_of(item, helper):10}] at"
            f" {helper}...",
        )

        created_id: str | None = None
        try:
            item_created = item_side.add_item(item)
            item_created_id = str(item_created[helper.id_key])
            created_id = item_created_id
            self._log_stored_change(
                helper=helper,
                item_id=item_created_id,
                operation="created",
                result="succeeded",
                before=None,
                after=item_created,
                requested=item,
            )
            with self._logged_task_context(helper, item_created_id, item_created, item):
                source_tw_uuid = self._checkpoint_source_identity(
                    item,
                    helper,
                    item_created_id,
                )
                self._run_post_create_sync(item_side, item_created_id, item)
                self._clear_pending_comments(source_tw_uuid, helper)

            logger.debug(f'Pickling newly created {helper} item -> "{item_created_id}"')
            pickle_dump(item_created, serdes_dir / item_created_id)
            self._written_serdes.add((helper.name, item_created_id))
            self._advance_operation_progress()
        except Exception as exc:
            self._record_operation_failure(helper, "Create", exc)
            if created_id is None:
                self._log_stored_change(
                    helper=helper,
                    item_id="",
                    operation="created",
                    result="failed",
                    before=None,
                    after=None,
                    requested=item,
                    error=exc,
                )
            else:
                self._log_stored_change(
                    helper=helper,
                    item_id=created_id,
                    operation="follow-up",
                    result="failed",
                    before=None,
                    after=None,
                    requested=item,
                    error=exc,
                    note="The task was created, then a later step failed.",
                )
            raise

        return item_created_id

    def _read_back_updated_item(
        self,
        side: SyncSide,
        item_id: ID,
        helper: SideHelper,
    ) -> Item:
        """Read back an updated item, failing if the target can no longer be retrieved."""
        current_target = side.get_item(item_id, use_cached=False)
        if current_target is None:
            raise RuntimeError(
                f"[{helper}] Updated item {item_id} could not be read back.",
            )
        return current_target

    def updater_to(self, item_id: ID, item: Item, helper: SideHelper):
        """Update an item using the given side helper."""
        side, _ = self._get_side_instances(helper)
        serdes_dir, _ = self._get_serdes_dirs(helper)
        logger.debug(
            f"[{helper.other}] Updating item [{self._summary_of(item, helper):10}] at"
            f" {helper}...",
        )

        before = self._snapshot_for_log(side, item_id)
        try:
            side.update_item(item_id, **item)
            current_target = self._read_back_updated_item(side, item_id, helper)
            pickle_dump(current_target, serdes_dir / item_id)
            self._written_serdes.add((helper.name, item_id))
            self._log_stored_change(
                helper=helper,
                item_id=item_id,
                operation="updated",
                result="succeeded",
                before=before,
                after=current_target,
                requested=item,
            )
            self._advance_operation_progress()
        except Exception as exc:
            self._record_operation_failure(helper, "Update", exc)
            observed = None
            try:
                observed = side.get_item(item_id, use_cached=False)
                if observed is not None:
                    pickle_dump(observed, serdes_dir / item_id)
                    self._written_serdes.add((helper.name, item_id))
            except Exception:  # noqa: BLE001
                # This checkpoint is best-effort after an existing sync failure; never let a
                # secondary connector/cache error mask the original exception being re-raised.
                logger.opt(exception=True).warning(
                    f"[{helper}] Could not checkpoint target state after a failed update.",
                )
            self._log_stored_change(
                helper=helper,
                item_id=item_id,
                operation="updated",
                result="failed",
                before=before,
                after=observed,
                requested=item,
                error=exc,
                note="Stored state below is whatever could be read back after the error.",
            )
            raise

    def deleter_to(self, item_id: ID, helper: SideHelper):
        """Delete an item using the given side helper."""
        logger.debug(f"[{helper}] Synchronising deleted item, id -> {item_id}...")
        side, _ = self._get_side_instances(helper)
        before = self._snapshot_for_log(side, item_id)
        try:
            side.delete_single_item(item_id)
            self._remove_serdes_files(helper=helper, ids=(item_id,))
            self._log_stored_change(
                helper=helper,
                item_id=item_id,
                operation="deleted",
                result="succeeded",
                before=before,
                after=None,
                requested=None,
            )
            self._advance_operation_progress()
        except Exception as exc:
            self._record_operation_failure(helper, "Delete", exc)
            self._log_stored_change(
                helper=helper,
                item_id=item_id,
                operation="deleted",
                result="failed",
                before=before,
                after=None,
                requested=None,
                error=exc,
            )
            raise

    def _snapshot_for_log(self, side: SyncSide, item_id: ID) -> Item | None:
        """Read the stored item before a write so the log can show a real diff."""
        if getattr(self, "change_log", None) is None:
            return None
        try:
            return side.get_item(item_id, use_cached=False)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                f"Could not read item {item_id} before logging a sync write.",
            )
            return None

    def _logged_task_context(self, helper: SideHelper, item_id: ID, *items: Item | None):
        change_log = getattr(self, "change_log", None)
        if change_log is None:
            return nullcontext()
        tw_uuid, asana_gid = self._log_identities(helper, item_id, *items)
        task_name = ""
        for item in items:
            task_name = task_name_from_item(item)
            if task_name:
                break
        return change_log.task_context(
            tw_uuid=tw_uuid,
            asana_gid=asana_gid,
            task_name=task_name,
        )

    def _log_identities(
        self,
        helper: SideHelper,
        item_id: ID,
        *items: Item | None,
    ) -> tuple[str | None, str | None]:
        counterpart = None
        try:
            counterpart = self._get_ids_map(helper).get(item_id)
        except (AttributeError, KeyError, TypeError):
            counterpart = None
        if helper.name == "Asana":
            asana_gid = str(item_id) if item_id else None
            tw_uuid = str(counterpart) if counterpart is not None else None
        else:
            tw_uuid = str(item_id) if item_id else None
            asana_gid = str(counterpart) if counterpart is not None else None
        for item in items:
            if tw_uuid is None:
                source_uuid = getattr(item, "source_tw_uuid", None)
                if source_uuid is None and isinstance(item, dict):
                    source_uuid = item.get("uuid") or item.get("source_tw_uuid")
                if source_uuid is not None:
                    tw_uuid = str(source_uuid)
            if asana_gid is None and isinstance(item, dict) and item.get("asana_gid"):
                asana_gid = str(item["asana_gid"])
            if asana_gid is None and helper.name == "Asana":
                gid = getattr(item, "gid", None)
                if gid:
                    asana_gid = str(gid)
        return tw_uuid, asana_gid

    def _log_stored_change(
        self,
        *,
        helper: SideHelper,
        item_id: ID,
        operation: str,
        result: str,
        before: Item | None,
        after: Item | None,
        requested: Item | None,
        error: Exception | None = None,
        note: str | None = None,
    ) -> None:
        """Record a create, update, or delete from the stored before/after items."""
        change_log = getattr(self, "change_log", None)
        if change_log is None:
            return

        asana = helper.name == "Asana"
        if operation == "created" and result == "succeeded":
            fields, texts = created_item_changes(after, asana=asana)
        elif operation == "deleted" and result == "succeeded":
            fields, texts = diff_stored_items(before, {}, asana=asana)
        elif result == "failed":
            fields, texts = diff_stored_items(before, after, asana=asana)
        else:
            fields, texts = diff_stored_items(before, after, asana=asana)

        tw_uuid, asana_gid = self._log_identities(helper, item_id, after, before, requested)
        task_name = (
            task_name_from_item(after)
            or task_name_from_item(before)
            or task_name_from_item(requested)
        )
        change_log.record(
            direction=TO_ASANA if asana else TO_TASKWARRIOR,
            operation=operation,
            result=result,
            tw_uuid=tw_uuid,
            asana_gid=asana_gid,
            task_name=task_name,
            fields=fields,
            texts=texts,
            note=note,
            error=f"{type(error).__name__}: {error}" if error is not None else None,
        )

    def item_getter_for(self, item_id: ID, helper: SideHelper) -> Item:
        """Return an item from the current sync snapshot, falling back to the live side."""
        snapshot = self._items_B if helper is self._helper_B else self._items_A
        if item_id in snapshot:
            return snapshot[item_id]

        logger.debug(f"Fetching {helper} item for id -> {item_id}")
        side, _ = self._get_side_instances(helper)
        return side.get_item(item_id)

    def _item_has_update(self, prev_item: Item, new_item: Item, helper: SideHelper) -> bool:
        """Determine whether the item has been updated."""
        side, _ = self._get_side_instances(helper)

        return not side.items_are_identical(
            prev_item,
            new_item,
            ignore_keys=[helper.id_key, *helper.ignore_keys],
        )

    def _get_ids_map(self, helper: SideHelper):
        return self._B_to_A_map if helper is self._helper_B else self._B_to_A_map.inverse

    def _get_serdes_dirs(self, helper: SideHelper) -> tuple[Path, Path]:
        serdes_dir = self.config[f"{helper}_serdes"]
        other_serdes_dir = self.config[f"{helper.other}_serdes"]

        return serdes_dir, other_serdes_dir

    def _get_side_instances(self, helper: SideHelper) -> tuple[SyncSide, SyncSide]:
        side = self._side_B if helper is self._helper_B else self._side_A
        other_side = self._side_A if helper is self._helper_B else self._side_B

        return side, other_side

    def _remove_serdes_files(self, helper: SideHelper, *, ids: Iterable[ID]):
        serdes_dir, _ = self._get_serdes_dirs(helper)

        def full_path(id_: ID) -> Path:
            return serdes_dir / str(id_)

        for id_ in ids:
            p = full_path(id_)
            try:
                p.unlink()
            except FileNotFoundError:
                logger.warning(f"File doesn't exist, this may indicate an error -> {p}")
                logger.opt(exception=True).debug(
                    f"File doesn't exist, this may indicate an error -> {p}",
                )

    def _summary_of(self, item: Item, helper: SideHelper, short=True) -> str:
        """Get the summary of the given item."""
        ret = item[helper.summary_key]
        if short:
            return ret[:10]

        return ret
