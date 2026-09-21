import json
from collections.abc import Sequence
from pathlib import Path

import asana
from bubop import logger
from rich.console import Console

from syncall.asana.asana_task import AsanaComment, AsanaTask
from syncall.asana.rich_text import asana_html_to_markdown
from syncall.progress import make_progress
from syncall.sync_side import SyncSide
from syncall.types import AsanaGID

GET_TASKS_PAGE_SIZE = 100
TASK_FIELDS = [
    "completed",
    "completed_at",
    "created_at",
    "due_at",
    "due_on",
    "gid",
    "html_notes",
    "modified_at",
    "name",
]
STORY_FIELDS = ["created_at", "gid", "resource_subtype", "text", "type"]


class AsanaSide(SyncSide):
    """Wrapper class to add/modify/delete Asana tasks."""

    def __init__(
        self,
        client: asana.Client,
        task_gid: AsanaGID,
        workspace_gid: AsanaGID,
        comment_cache_path: Path | None = None,
    ):
        self._client = client
        self._task_gid = task_gid
        self._workspace_gid = workspace_gid
        self._comment_cache_path = comment_cache_path
        self._comment_cache = self._load_comment_cache()
        self._comment_cache_dirty = False

        super().__init__(name="Asana", fullname="Asana")

    def start(self):
        pass

    def finish(self):
        self._save_comment_cache()

    def _load_comment_cache(self) -> dict[str, dict]:
        if self._comment_cache_path is None or not self._comment_cache_path.is_file():
            return {}

        try:
            return json.loads(self._comment_cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning(
                f"Could not read Asana comment cache at {self._comment_cache_path}; rebuilding it.",
            )
            return {}

    def _save_comment_cache(self) -> None:
        if self._comment_cache_path is None or not self._comment_cache_dirty:
            return

        self._comment_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._comment_cache_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(self._comment_cache, ensure_ascii=False))
        temp_path.replace(self._comment_cache_path)
        self._comment_cache_dirty = False

    def _get_follower_task_summaries(self) -> list[dict]:
        """Fetch all follower-only tasks using Asana search's manual pagination."""
        results: list[dict] = []
        created_after = None

        while True:
            params = {
                "followers.any": "me",
                "assignee.not": "me",
                "sort_by": "created_at",
                "sort_ascending": True,
            }
            if created_after is not None:
                params["created_at.after"] = created_after

            page = list(
                self._client.tasks.search_in_workspace(
                    self._workspace_gid,
                    params=params,
                    fields=TASK_FIELDS,
                    page_size=GET_TASKS_PAGE_SIZE,
                ),
            )
            results.extend(page)

            if len(page) < GET_TASKS_PAGE_SIZE:
                break

            next_created_after = page[-1].get("created_at")
            if not next_created_after or next_created_after == created_after:
                raise RuntimeError(
                    "Could not advance Asana follower-task search pagination by created_at.",
                )
            created_after = next_created_after

        return results

    def _get_task_summaries(self) -> list[dict]:
        """Return assigned tasks plus follower-only tasks, deduplicated by GID."""
        assigned = self._client.tasks.find_all(
            assignee="me",
            workspace=self._workspace_gid,
            fields=TASK_FIELDS,
            page_size=GET_TASKS_PAGE_SIZE,
        )

        by_gid = {str(task["gid"]): task for task in assigned}

        try:
            followed = self._get_follower_task_summaries()
            for task in followed:
                by_gid[str(task["gid"])] = task
        except asana.error.PremiumOnlyError as exc:
            raise RuntimeError(
                "Asana follower task discovery requires access to the workspace task search API.",
            ) from exc

        return list(by_gid.values())

    def get_all_items(self, **kwargs) -> Sequence[AsanaTask]:
        del kwargs
        results = []

        if self._task_gid is None:
            console = Console()
            with console.status("[bold]Discovering Asana tasks...[/bold]", spinner="dots"):
                raw_tasks = self._get_task_summaries()

            total = len(raw_tasks)
            console.print(f"[bold]Found {total:,} Asana tasks[/bold]")

            progress = make_progress(console=console, unit="tasks")
            with progress:
                progress_task = progress.add_task("Loading Asana history", total=total)
                for index, discovered_task in enumerate(raw_tasks, start=1):
                    raw_task = dict(discovered_task)
                    raw_task["comments"] = self._get_cached_comments(raw_task)
                    results.append(AsanaTask.from_raw_task(raw_task))
                    progress.advance(progress_task)

                    if index % 100 == 0:
                        self._save_comment_cache()

            self._save_comment_cache()
        else:
            task = self.get_item(self._task_gid)
            if task is not None:
                results.append(task)

        return results

    def _get_comments(self, item_id: AsanaGID) -> tuple[AsanaComment, ...]:
        stories = self._client.tasks.stories(
            item_id,
            fields=STORY_FIELDS,
            page_size=GET_TASKS_PAGE_SIZE,
        )
        comments = []
        for story in stories:
            is_comment = (
                story.get("type") == "comment"
                or story.get("resource_subtype") == "comment_added"
            )
            text = story.get("text")
            if is_comment and text:
                comments.append(AsanaComment.from_raw(story))
        return tuple(comments)

    def _get_cached_comments(self, raw_task: dict) -> tuple[AsanaComment, ...]:
        item_id = str(raw_task["gid"])
        modified_at = str(raw_task.get("modified_at") or "")

        cached = self._comment_cache.get(item_id)
        if cached is not None and cached.get("modified_at") == modified_at:
            cached_comments = cached.get("comments", ())
            if cached.get("version") == 2:
                return tuple(AsanaComment.from_raw(comment) for comment in cached_comments)

            # Legacy caches stored only comment text. Empty entries can be upgraded without
            # another API request; non-empty entries need one refresh to recover GIDs/dates.
            if not cached_comments:
                cached["version"] = 2
                self._comment_cache_dirty = True
                return ()

        comments = self._get_comments(item_id)
        if self._comment_cache_path is not None:
            self._comment_cache[item_id] = {
                "version": 2,
                "modified_at": modified_at,
                "comments": [comment.to_cache() for comment in comments],
            }
            self._comment_cache_dirty = True
        return comments

    def _add_missing_comments(self, item_id: AsanaGID, comments: Sequence[str]) -> None:
        existing = {comment.text for comment in self._get_comments(item_id)}
        for comment in comments:
            comment_text = str(comment).strip()
            if comment_text and comment_text not in existing:
                self._client.tasks.add_comment(item_id, text=comment_text)
                existing.add(comment_text)

    def get_item(self, item_id: AsanaGID) -> AsanaTask | None:
        """Get a single task based on the given ID."""
        try:
            raw_task = self._client.tasks.find_by_id(item_id, fields=TASK_FIELDS)
            raw_task["comments"] = self._get_cached_comments(raw_task)
            return AsanaTask.from_raw_task(raw_task)
        except asana.error.ForbiddenError as exc:
            raise RuntimeError(
                f"Asana task {item_id} is not accessible; refusing to treat it as deleted.",
            ) from exc
        except asana.error.NotFoundError:
            return None

    def delete_single_item(self, item_id: AsanaGID):
        self._client.tasks.delete_task(item_id)

    def update_item(self, item_id: AsanaGID, **changes):
        """Update an existing task and append any missing comments."""
        desired_comments = tuple(str(comment) for comment in changes.get("comments", ()))
        raw_task = AsanaTask(**changes).to_raw_task()

        raw_task.pop("completed_at", None)
        raw_task.pop("created_at", None)
        raw_task.pop("gid", None)
        raw_task.pop("modified_at", None)

        remote_task = self.get_item(item_id)
        if remote_task is None:
            raise RuntimeError(f"Asana task {item_id} disappeared while updating it.")

        desired_html_notes = raw_task.get("html_notes")
        if desired_html_notes is not None and asana_html_to_markdown(
            desired_html_notes,
        ) == asana_html_to_markdown(remote_task.html_notes):
            # Preserve Asana-only rich-text metadata (for example true mentions)
            # when the Markdown representation was not actually edited.
            raw_task.pop("html_notes", None)

        if remote_task.get("due_on", None) is None:
            raw_task.pop("due_on", None)
        elif remote_task.get("due_at", None) is None:
            raw_task.pop("due_at", None)
        else:
            raw_task.pop("due_on", None)

        self._client.tasks.update_task(item_id, raw_task)
        self._add_missing_comments(item_id, desired_comments)

    def add_item(self, item: AsanaTask) -> AsanaTask:
        """Add a new task, then append Taskwarrior annotations as comments."""
        raw_task = item.to_raw_task()
        desired_comments = item.comments

        if "assignee" not in raw_task:
            raw_task["assignee"] = "me"

        if "workspace" not in raw_task:
            raw_task["workspace"] = self._workspace_gid

        raw_task.pop("created_at", None)
        raw_task.pop("modified_at", None)
        raw_task.pop("gid", None)
        raw_task.pop("due_on", None)

        created = self._client.tasks.create_task(raw_task)
        item_id = created["gid"]
        self._add_missing_comments(item_id, desired_comments)

        refreshed = self.get_item(item_id)
        if refreshed is None:
            raise RuntimeError(f"Failed to retrieve newly created Asana task {item_id}.")
        return refreshed

    @classmethod
    def id_key(cls) -> str:
        return "gid"

    @classmethod
    def summary_key(cls) -> str:
        return "name"

    @classmethod
    def last_modification_key(cls) -> str:
        return "modified_at"

    @classmethod
    def items_are_identical(
        cls,
        item1: AsanaTask,
        item2: AsanaTask,
        ignore_keys: Sequence[str] = [],
    ) -> bool:
        compare_keys = set(AsanaTask._key_names)

        for key in ignore_keys:
            compare_keys.discard(key)

        if item1.get("due_at", None) is not None and item2.get("due_at", None) is not None:
            compare_keys.discard("due_on")
        elif item1.get("due_on", None) is not None and item2.get("due_on", None) is not None:
            compare_keys.discard("due_at")

        return SyncSide._items_are_identical(item1, item2, compare_keys)
