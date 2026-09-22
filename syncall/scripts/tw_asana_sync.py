from __future__ import annotations

import fcntl
import sys

import asana
import click
from bubop import (
    check_optional_mutually_exclusive,
    check_required_mutually_exclusive,
    format_dict,
    logger,
    loguru_tqdm_sink,
)
from rich.console import Console
from xdg import xdg_config_home

from syncall.app_utils import confirm_before_proceeding, inform_about_app_extras

try:
    from syncall.asana.asana_side import AsanaSide
    from syncall.asana.utils import list_asana_workspaces
    from syncall.taskwarrior.taskwarrior_side import TaskWarriorSide
except ImportError:
    inform_about_app_extras(["asana", "tw"])


from syncall.aggregator import Aggregator
from syncall.app_utils import (
    app_log_to_syslog,
    cache_or_reuse_cached_combination,
    determine_app_config_fname,
    error_and_exit,
    fetch_app_configuration,
    get_resolution_strategy,
    register_teardown_handler,
)
from syncall.cli import opts_asana, opts_miscellaneous, opts_tw_filtering
from syncall.progress import make_progress
from syncall.tw_asana_utils import convert_asana_to_tw, convert_tw_to_asana


def _acquire_sync_lock():
    """Prevent overlapping local sync processes from racing writes."""
    lock_path = xdg_config_home() / "syncall" / "tw_asana_sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        error_and_exit("Another Taskwarrior ↔ Asana sync is already running.")
    return handle


def resume_pending_asana_comments(
    tw_side: TaskWarriorSide,
    asana_side: AsanaSide,
    items: list[dict],
    *,
    console: Console | None = None,
) -> int:
    """Resume interrupted TW→Asana comment creation from Taskwarrior-owned markers."""
    pending_items = [
        item
        for item in items
        if item.get("asana_pending_comments") and item.get("asana_gid")
    ]
    if not pending_items:
        return 0

    progress = make_progress(console=console or Console(), unit="tasks")
    with progress:
        task_id = progress.add_task(
            "Resuming interrupted comment sync",
            total=len(pending_items),
        )
        for item in pending_items:
            desired_asana = convert_tw_to_asana(item)
            asana_side.ensure_comments(
                str(item["asana_gid"]),
                desired_asana.comments,
            )
            tw_side.clear_pending_asana_comments(str(item["uuid"]))
            progress.advance(task_id)

    return len(pending_items)


# CLI parsing ---------------------------------------------------------------------------------
@click.command()
@opts_asana(hidden_gid=False)
@opts_tw_filtering()
@opts_miscellaneous("TW", "Asana")
def main(  # noqa: PLR0915, C901, PLR0912
    asana_task_gid: str,
    asana_token: str,
    asana_workspace_gid: str,
    asana_workspace_name: str,
    do_list_asana_workspaces: bool,
    tw_filter: str,
    tw_tags: list[str],
    tw_project: str,
    tw_only_modified_last_X_days: str,
    tw_sync_all_tasks: bool,
    prefer_scheduled_date: bool,
    resolution_strategy: str,
    verbose: int,
    combination_name: str,
    custom_combination_savename: str,
    pdb_on_error: bool,
    confirm: bool,
):
    """Synchronize your tasks in Asana with filters from Taskwarrior."""
    del prefer_scheduled_date

    loguru_tqdm_sink(verbosity=verbose)
    app_log_to_syslog()
    logger.debug("Initialising...")
    sync_lock = _acquire_sync_lock()
    inform_about_config = False

    # cli validation --------------------------------------------------------------------------
    check_optional_mutually_exclusive(combination_name, custom_combination_savename)

    tw_filter_li = [
        t
        for t in [
            tw_filter,
            tw_only_modified_last_X_days,
        ]
        if t
    ]

    combination_of_tw_filters_and_asana_workspace = any(
        [
            tw_filter_li,
            tw_tags,
            tw_project,
            tw_sync_all_tasks,
            asana_workspace_gid,
            asana_workspace_name,
        ],
    )
    check_optional_mutually_exclusive(
        combination_name,
        combination_of_tw_filters_and_asana_workspace,
    )

    # existing combination name is provided ---------------------------------------------------
    if combination_name is not None:
        app_config = fetch_app_configuration(
            side_A_name="Taskwarrior",
            side_B_name="Asana",
            combination=combination_name,
        )
        tw_tags = app_config["tw_tags"]
        tw_project = app_config["tw_project"]
        tw_sync_all_tasks = app_config.get("tw_sync_all_tasks", False)
        asana_workspace_gid = app_config["asana_workspace_gid"]
        asana_task_gid = app_config["asana_task_gid"]
        resolution_strategy = app_config.get("resolution_strategy", resolution_strategy)
    # combination manually specified ----------------------------------------------------------
    else:
        inform_about_config = True
        combination_name = cache_or_reuse_cached_combination(
            config_args={
                "asana_workspace_gid": asana_workspace_gid,
                "tw_project": tw_project,
                "tw_tags": tw_tags,
                "tw_sync_all_tasks": tw_sync_all_tasks,
                "asana_task_gid": asana_task_gid,
                "resolution_strategy": resolution_strategy,
            },
            config_fname=determine_app_config_fname("Taskwarrior", "Asana"),
            custom_combination_savename=custom_combination_savename,
        )

    # initialize asana -----------------------------------------------------------------------
    asana_client = asana.Client.access_token(asana_token)
    asana_client.headers["Asana-Disable"] = ",".join(
        [
            asana_client.headers.get("Asana-Disable", ""),
            "new_user_task_lists",
            "new_goal_memberships",
        ],
    )
    asana_client.options["client_name"] = "syncall"

    # list workspaces and exit
    if do_list_asana_workspaces:
        list_asana_workspaces(asana_client)
        return 0

    # asana workspaces-------------------------------------------------------------------------
    # Validate Asana workspace selection. Skip this only if we are going to
    # --list-asana-workspaces or if --asana-task-gid was not specified.
    if asana_task_gid is None:
        if asana_workspace_gid is None:
            if asana_workspace_name is None:
                error_and_exit("Provide either an Asana workspace name or GID to sync.")
        elif asana_workspace_name is not None:
            error_and_exit("Provide either Asana workspace GID or name, but not both.")

        found_workspace = False

        for workspace in asana_client.workspaces.find_all():  # type: ignore
            if workspace["gid"] == asana_workspace_gid:
                asana_workspace_name = workspace["name"]
                found_workspace = True
                break
            if workspace["name"] == asana_workspace_name:
                if found_workspace:
                    error_and_exit(
                        f"Found multiple workspaces with name {asana_workspace_name}. Please"
                        " specify workspace GID instead.",
                    )
                else:
                    asana_workspace_gid = workspace["gid"]
                    found_workspace = True
        else:
            if not asana_workspace_gid:
                li = [f"No Asana workspace was found with GID {asana_workspace_gid}"]
                if asana_workspace_name:
                    li.append(f" | Workspace Name: {asana_workspace_name}")
                error_and_exit(f"{' '.join(li)}.")

    # more checks -----------------------------------------------------------------------------
    combination_of_tw_related_options = any([tw_filter_li, tw_tags, tw_project])
    check_required_mutually_exclusive(
        tw_sync_all_tasks,
        combination_of_tw_related_options,
        "sync_all_tw_tasks",
        "combination of specific TW-related options",
    )

    # announce configuration ------------------------------------------------------------------
    logger.info(
        format_dict(
            header="Configuration",
            items={
                "TW Filter": " ".join(tw_filter_li),
                "TW Tags": tw_tags,
                "TW Project": tw_project,
                "TW Sync All Tasks": tw_sync_all_tasks,
                "Asana Workspace GID": asana_workspace_gid,
                "Asana Workspace Name": asana_workspace_name,
                "Asana Task GID": asana_task_gid,
                "Resolution Strategy": resolution_strategy,
            },
            prefix="\n\n",
            suffix="\n",
        ),
    )
    if confirm:
        confirm_before_proceeding()

    # initialize sides ------------------------------------------------------------------------
    tw_side = TaskWarriorSide(
        tw_filter=" ".join(tw_filter_li),
        tags=tw_tags,
        project=tw_project,
    )

    asana_side = AsanaSide(
        client=asana_client,
        task_gid=asana_task_gid,
        workspace_gid=asana_workspace_gid,
        comment_cache_path=xdg_config_home() / "syncall" / "asana_comments.json",
    )

    # teardown function and exception handling ------------------------------------------------
    register_teardown_handler(
        pdb_on_error=pdb_on_error,
        inform_about_config=inform_about_config,
        combination_name=combination_name,
        verbose=verbose,
    )

    # sync ------------------------------------------------------------------------------------
    with Aggregator(
        side_A=asana_side,
        side_B=tw_side,
        converter_A_to_B=convert_asana_to_tw,
        converter_B_to_A=convert_tw_to_asana,
        resolution_strategy=get_resolution_strategy(
            resolution_strategy,
            side_A_type=type(asana_side),
            side_B_type=type(tw_side),
        ),
        config_fname=combination_name,
        ignore_keys=(
            (
                "completed_at",
                "created_at",
                "modified_at",
            ),
            ("end", "entry", "modified", "urgency"),
        ),
    ) as aggregator:
        console = Console()
        with console.status("[bold]Loading Taskwarrior snapshot...[/bold]", spinner="dots"):
            existing_tw_items = tw_side.get_all_items()
        console.print(
            f"[bold]Found {len(existing_tw_items):,} Taskwarrior tasks in sync scope[/bold]"
        )

        # Resume any interrupted TW→Asana task creation before normal change detection.
        # The marker lives on the Taskwarrior task itself, so this does not depend on
        # syncall's cache or preference files.
        resumed_pending = resume_pending_asana_comments(
            tw_side,
            asana_side,
            existing_tw_items,
            console=console,
        )
        if resumed_pending:
            logger.info(
                f"Resumed comment synchronization for {resumed_pending} interrupted "
                "Taskwarrior→Asana task(s).",
            )

        existing_tw_items = tw_side.get_all_items()
        recovered = {
            str(item["uuid"]): str(item["asana_gid"])
            for item in existing_tw_items
            if item.get("asana_gid")
        }
        recovered_count = aggregator.recover_correspondences(recovered)
        if recovered_count:
            logger.info(
                f"Recovered {recovered_count} Asana↔Taskwarrior task mapping(s) "
                "from Taskwarrior.",
            )
            aggregator.flush_correspondences()

        # Backfill all current mapped identities before doing any network writes. This makes
        # the mapping reconstructable even if syncall preference/cache files are later lost.
        with console.status(
            "[bold]Persisting Asana task identities in Taskwarrior...[/bold]",
            spinner="dots",
        ):
            backfilled = tw_side.backfill_asana_gids(
                {
                    str(tw_id): str(asana_id)
                    for tw_id, asana_id in aggregator._B_to_A_map.items()
                },
            )
        if backfilled:
            logger.info(f"Persisted Asana identity on {backfilled} Taskwarrior task(s).")

        aggregator.sync()
        aggregator.flush_correspondences()

        # Backfill any mappings created during this sync too.
        with console.status(
            "[bold]Persisting newly created Asana task identities...[/bold]",
            spinner="dots",
        ):
            post_sync_backfilled = tw_side.backfill_asana_gids(
                {
                    str(tw_id): str(asana_id)
                    for tw_id, asana_id in aggregator._B_to_A_map.items()
                },
            )
        if post_sync_backfilled:
            logger.info(
                f"Persisted Asana identity on {post_sync_backfilled} newly mapped "
                "Taskwarrior task(s).",
            )

        # Annotation entry timestamps are not part of taskw-ng's normal annotate/update path.
        # Reconcile them separately from cached Asana story metadata so interrupted migrations
        # are safe to resume and future runs become cheap no-ops once dates are correct.
        current_tw_items = {str(item["uuid"]): item for item in tw_side.get_all_items()}
        mapped_tasks = tuple(aggregator._B_to_A_map.items())
        repaired_annotations = 0
        reconciliation_progress = make_progress(console=console, unit="tasks")
        with reconciliation_progress:
            reconciliation_task = reconciliation_progress.add_task(
                "Checking annotation history",
                total=len(mapped_tasks),
            )
            for tw_id, asana_id in mapped_tasks:
                asana_item = aggregator._items_A.get(str(asana_id))
                if asana_item is not None:
                    desired_tw_item = convert_asana_to_tw(asana_item)
                    if desired_tw_item is not None:
                        current_tw_item = current_tw_items.get(str(tw_id))
                        current_annotations = (
                            current_tw_item.get("annotations", ())
                            if current_tw_item is not None
                            else None
                        )
                        repaired_annotations += tw_side.reconcile_annotation_timestamps(
                            str(tw_id),
                            desired_tw_item.get("annotations", ()),
                            current_annotations=current_annotations,
                        )
                reconciliation_progress.advance(reconciliation_task)

        if repaired_annotations:
            logger.info(
                f"Repaired {repaired_annotations} historical Taskwarrior annotation "
                "timestamp(s) from Asana.",
            )

    sync_lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
