from collections.abc import Sequence

import asana
from syncall.asana.asana_task import AsanaTask
from syncall.asana.rich_text import asana_html_to_markdown
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
STORY_FIELDS = ["gid", "resource_subtype", "text", "type"]


class AsanaSide(SyncSide):
    """Wrapper class to add/modify/delete Asana tasks."""

    def __init__(self, client: asana.Client, task_gid: AsanaGID, workspace_gid: AsanaGID):
        self._client = client
        self._task_gid = task_gid
        self._workspace_gid = workspace_gid

        super().__init__(name="Asana", fullname="Asana")

    def start(self):
        pass

    def finish(self):
        pass

    def _get_task_summaries(self) -> list[dict]:
        """Return assigned tasks plus follower-only tasks, deduplicated by GID."""
        assigned = self._client.tasks.find_all(
            assignee="me",
            workspace=self._workspace_gid,
            page_size=GET_TASKS_PAGE_SIZE,
        )

        by_gid = {str(task["gid"]): task for task in assigned}

        try:
            followed = self._client.tasks.search_in_workspace(
                self._workspace_gid,
                params={
                    "followers.any": "me",
                    "assignee.not": "me",
                },
                page_size=GET_TASKS_PAGE_SIZE,
            )
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
            for task in self._get_task_summaries():
                detailed_task = self.get_item(task["gid"])
                if detailed_task is not None:
                    results.append(detailed_task)
        else:
            task = self.get_item(self._task_gid)
            if task is not None:
                results.append(task)

        return results

    def _get_comments(self, item_id: AsanaGID) -> tuple[str, ...]:
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
                comments.append(str(text))
        return tuple(comments)

    def _add_missing_comments(self, item_id: AsanaGID, comments: Sequence[str]) -> None:
        existing = set(self._get_comments(item_id))
        for comment in comments:
            comment = str(comment).strip()
            if comment and comment not in existing:
                self._client.tasks.add_comment(item_id, text=comment)
                existing.add(comment)

    def get_item(self, item_id: AsanaGID) -> AsanaTask | None:
        """Get a single task based on the given ID."""
        try:
            raw_task = self._client.tasks.find_by_id(item_id, fields=TASK_FIELDS)
            raw_task["comments"] = self._get_comments(item_id)
            return AsanaTask.from_raw_task(raw_task)
        except asana.error.ForbiddenError:
            return None
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
        compare_keys = AsanaTask._key_names.copy()

        for key in ignore_keys:
            if key in compare_keys:
                compare_keys.remove(key)

        if item1.get("due_at", None) is not None and item2.get("due_at", None) is not None:
            compare_keys.remove("due_on")
        elif item1.get("due_on", None) is not None and item2.get("due_on", None) is not None:
            compare_keys.remove("due_at")

        return SyncSide._items_are_identical(item1, item2, compare_keys)
