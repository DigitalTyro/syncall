from __future__ import annotations

from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from item_synchronizer.types import ID, ConverterFn, Item

    from syncall.sync_side import SyncSide

from functools import partial
from typing import Any

from bidict import bidict  # pyright: ignore[reportPrivateImportUsage]
from bubop import PrefsManager, logger, pickle_dump, pickle_load
from item_synchronizer import Synchronizer
from item_synchronizer.helpers import SideChanges
from item_synchronizer.resolution_strategy import AlwaysSecondRS, ResolutionStrategy
from rich.console import Console

from syncall.app_utils import app_name
from syncall.progress import make_progress
from syncall.side_helper import SideHelper


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
            converter_to_A=converter_B_to_A,
            converter_to_B=converter_A_to_B,
            item_getter_A=side_A_fn(self.item_getter_for),
            item_getter_B=side_B_fn(self.item_getter_for),
            resolution_strategy=self._resolution_strategy,
            catch_exceptions=catch_exceptions,
            side_names=(side_A.fullname, side_B.fullname),
        )

        self._items_A: dict[ID, Item] = {}
        self._items_B: dict[ID, Item] = {}
        self._operation_progress = None
        self._operation_progress_task = None
        self._operation_failed = False
        self._written_serdes: set[tuple[str, ID]] = set()
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
            raise RuntimeError(
                "One or more sync writes failed; sync state was not committed so the "
                "next run can retry safely.",
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

        try:
            item_created = item_side.add_item(item)
            item_created_id = str(item_created[helper.id_key])

            source_tw_uuid = getattr(item, "source_tw_uuid", None)
            if source_tw_uuid is not None:
                source_tw_uuid = str(source_tw_uuid)
                _, source_side = self._get_side_instances(helper)
                checkpoint_errors = []
                checkpoint_successes = 0

                try:
                    existing_asana_id = self._B_to_A_map.get(source_tw_uuid)
                    if existing_asana_id not in (None, item_created_id):
                        raise RuntimeError(
                            f"Taskwarrior task {source_tw_uuid} is already mapped to "
                            f"Asana task {existing_asana_id}.",
                        )
                    self._B_to_A_map[source_tw_uuid] = item_created_id
                    self.flush_correspondences()
                    checkpoint_successes += 1
                except Exception as exc:
                    checkpoint_errors.append(exc)

                record_asana_gid = getattr(source_side, "record_asana_gid", None)
                if callable(record_asana_gid):
                    try:
                        record_asana_gid(source_tw_uuid, item_created_id)
                        checkpoint_successes += 1
                    except Exception as exc:
                        checkpoint_errors.append(exc)

                if checkpoint_successes == 0:
                    cause = checkpoint_errors[-1] if checkpoint_errors else None
                    raise RuntimeError(
                        "Could not durably checkpoint the new Asana task identity; "
                        "refusing to create comments.",
                    ) from cause

            post_create_sync = getattr(item_side, "post_create_sync", None)
            if callable(post_create_sync):
                post_create_sync(item_created_id, item)

            if source_tw_uuid is not None:
                _, source_side = self._get_side_instances(helper)
                clear_pending = getattr(source_side, "clear_pending_asana_comments", None)
                if callable(clear_pending):
                    clear_pending(str(source_tw_uuid))

            # Cache both sides with pickle - f=id_
            logger.debug(f'Pickling newly created {helper} item -> "{item_created_id}"')
            pickle_dump(item_created, serdes_dir / item_created_id)
            self._written_serdes.add((helper.name, item_created_id))
            self._advance_operation_progress()
            return item_created_id
        except Exception:
            self._operation_failed = True
            raise

    def updater_to(self, item_id: ID, item: Item, helper: SideHelper):
        """Update an item using the given side helper."""
        side, _ = self._get_side_instances(helper)
        serdes_dir, _ = self._get_serdes_dirs(helper)
        logger.debug(
            f"[{helper.other}] Updating item [{self._summary_of(item, helper):10}] at"
            f" {helper}...",
        )

        try:
            side.update_item(item_id, **item)
            pickle_dump(item, serdes_dir / item_id)
            self._written_serdes.add((helper.name, item_id))
            self._advance_operation_progress()
        except Exception:
            self._operation_failed = True
            try:
                current_target = side.get_item(item_id, use_cached=False)
                if current_target is not None:
                    pickle_dump(current_target, serdes_dir / item_id)
                    self._written_serdes.add((helper.name, item_id))
            except Exception:
                logger.opt(exception=True).warning(
                    f"[{helper}] Could not checkpoint target state after a failed update.",
                )
            raise

    def deleter_to(self, item_id: ID, helper: SideHelper):
        """Delete an item using the given side helper."""
        logger.debug(f"[{helper}] Synchronising deleted item, id -> {item_id}...")
        side, _ = self._get_side_instances(helper)
        try:
            side.delete_single_item(item_id)
            self._remove_serdes_files(helper=helper, ids=(item_id,))
            self._advance_operation_progress()
        except Exception:
            self._operation_failed = True
            raise

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
