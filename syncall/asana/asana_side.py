import datetime
import json
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import asana
from bubop import logger
from rich.console import Console

from syncall.asana.asana_task import AsanaComment, AsanaTask
from syncall.progress import make_progress
from syncall.sync_side import SyncSide
from syncall.types import AsanaGID

GET_TASKS_PAGE_SIZE = 100
COMMENT_CACHE_MAX_AGE = datetime.timedelta(days=30)
COMMENT_CACHE_VERSION = 3
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
STORY_FIELDS = ["created_at", "gid", "html_text", "resource_subtype", "text", "type"]


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
            cache = json.loads(self._comment_cache_path.read_text())
        except (OSError, ValueError):
            logger.warning(
                f"Could not read Asana comment cache at {self._comment_cache_path}; rebuilding it.",
            )
            return {}

        if not isinstance(cache, dict):
            logger.warning(
                f"Could not read Asana comment cache at {self._comment_cache_path}; rebuilding it.",
            )
            return {}

        return cache

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
            if cached.get("version") == COMMENT_CACHE_VERSION:
                structured_comments = tuple(
                    AsanaComment.from_raw(comment) for comment in cached_comments
                )
                if not structured_comments:
                    return ()

                checked_at = cached.get("checked_at")
                last_checked = None
                if checked_at is not None:
                    try:
                        last_checked = datetime.datetime.fromisoformat(str(checked_at))
                    except ValueError:
                        last_checked = None

                if last_checked is not None and last_checked.tzinfo is not None:
                    cache_age = datetime.datetime.now(datetime.UTC) - last_checked
                    if datetime.timedelta() <= cache_age < COMMENT_CACHE_MAX_AGE:
                        return structured_comments

            # Legacy caches stored only comment text. Empty entries can be upgraded without
            # another API request; non-empty entries need one refresh to recover GIDs/dates.
            elif not cached_comments:
                cached["version"] = COMMENT_CACHE_VERSION
                cached["checked_at"] = datetime.datetime.now(
                    datetime.UTC,
                ).isoformat()
                self._comment_cache_dirty = True
                return ()

        comments = self._get_comments(item_id)
        if self._comment_cache_path is not None:
            self._comment_cache[item_id] = {
                "version": COMMENT_CACHE_VERSION,
                "modified_at": modified_at,
                "checked_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "comments": [comment.to_cache() for comment in comments],
            }
            self._comment_cache_dirty = True
        return comments

    @staticmethod
    def _comment_key(comment: str | AsanaComment) -> str:
        text = unicodedata.normalize("NFC", str(comment))
        return " ".join(text.split())

    def _add_missing_comments(
        self,
        item_id: AsanaGID,
        comments: Sequence[str | AsanaComment],
    ) -> None:
        existing = {
            self._comment_key(comment)
            for comment in self._get_comments(item_id)
            if self._comment_key(comment)
        }
        for comment in comments:
            comment_text = str(comment).strip()
            comment_key = self._comment_key(comment)
            if not comment_key or comment_key in existing:
                continue

            # Never trust cache/state for a write. Re-read live Asana stories immediately
            # before creating a comment so stale state cannot create duplicates.
            live_existing = {
                self._comment_key(remote_comment)
                for remote_comment in self._get_comments(item_id)
                if self._comment_key(remote_comment)
            }
            existing.update(live_existing)
            if comment_key in existing:
                continue

            self._client.tasks.add_comment(item_id, text=comment_text)
            existing.add(comment_key)

    def get_item(
        self,
        item_id: AsanaGID,
        use_cached: bool = False,
    ) -> AsanaTask | None:
        """Get a single task based on the given ID."""
        del use_cached
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
        """Update safe scalar task fields without rewriting existing Asana rich text."""
        raw_task = AsanaTask(**changes).to_raw_task()

        raw_task.pop("completed_at", None)
        raw_task.pop("created_at", None)
        raw_task.pop("gid", None)
        raw_task.pop("modified_at", None)

        # Existing Asana rich text is canonical. Taskwarrior only stores a readable,
        # lossy projection of html_notes/comments, so writing that projection back would
        # destroy images, mentions, links and formatting. Existing descriptions and comments
        # are therefore intentionally one-way Asana -> Taskwarrior.
        raw_task.pop("html_notes", None)

        remote_task = self.get_item(item_id)
        if remote_task is None:
            raise RuntimeError(f"Asana task {item_id} disappeared while updating it.")
        remote_raw = remote_task.to_raw_task()

        if remote_task.get("due_on", None) is None:
            raw_task.pop("due_on", None)
        elif remote_task.get("due_at", None) is None:
            raw_task.pop("due_at", None)
        else:
            raw_task.pop("due_on", None)

        task_changes = {
            key: value for key, value in raw_task.items() if remote_raw.get(key) != value
        }
        if task_changes:
            self._client.tasks.update_task(item_id, task_changes)

    def add_item(self, item: AsanaTask) -> AsanaTask:
        """Create a task without comments so identity can be checkpointed first."""
        raw_task = item.to_raw_task()

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

        refreshed = self.get_item(item_id)
        if refreshed is None:
            raise RuntimeError(f"Failed to retrieve newly created Asana task {item_id}.")
        return refreshed

    def get_comments_live(self, item_id: AsanaGID) -> tuple[AsanaComment, ...]:
        """Return live Asana comments, bypassing the local comment cache."""
        return self._get_comments(item_id)

    def ensure_comments(
        self,
        item_id: AsanaGID,
        comments: Sequence[str | AsanaComment],
    ) -> None:
        """Ensure desired comments exist remotely using live duplicate checks."""
        self._add_missing_comments(item_id, comments)

    def post_create_sync(self, item_id: AsanaGID, item: AsanaTask) -> None:
        """Apply comments only after the caller has checkpointed task identity."""
        self.ensure_comments(item_id, item.comments)

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

        if "comments" in compare_keys:
            comments1 = [cls._comment_key(comment) for comment in item1.get("comments", ())]
            comments2 = [cls._comment_key(comment) for comment in item2.get("comments", ())]
            if comments1 != comments2:
                return False
            compare_keys.discard("comments")

        if item1.get("due_at", None) is not None and item2.get("due_at", None) is not None:
            compare_keys.discard("due_on")
        elif item1.get("due_on", None) is not None and item2.get("due_on", None) is not None:
            compare_keys.discard("due_at")

        return SyncSide._items_are_identical(item1, item2, compare_keys)
