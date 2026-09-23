#!/usr/bin/env python3
"""Read-only forensic report for Asana task/story activity in a time window."""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
from pathlib import Path

import asana
from dateutil.parser import isoparse

TASK_FIELDS = ["gid", "name", "modified_at", "completed", "completed_at", "due_on", "due_at"]
STORY_FIELDS = [
    "gid",
    "created_at",
    "created_by.gid",
    "created_by.name",
    "resource_subtype",
    "text",
    "source",
    "old_name",
    "new_name",
    "old_dates",
    "new_dates",
    "old_resource_subtype",
    "new_resource_subtype",
    "old_approval_status",
    "new_approval_status",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Asana forensic report for an ISO-8601 time window.",
    )
    parser.add_argument("--workspace", required=True, help="Asana workspace GID")
    parser.add_argument("--start", required=True, help="Inclusive ISO-8601 timestamp")
    parser.add_argument("--end", required=True, help="Inclusive ISO-8601 timestamp")
    parser.add_argument(
        "--output-prefix",
        default="asana-audit",
        help="Output prefix; writes <prefix>.tasks.csv, <prefix>.stories.csv and <prefix>.json",
    )
    return parser.parse_args()


def within(ts: str | None, start: datetime.datetime, end: datetime.datetime) -> bool:
    if not ts:
        return False
    value = isoparse(ts)
    return start <= value <= end


def make_story_row(task: dict, story: dict) -> dict:
    return {
        "task_gid": str(task["gid"]),
        "task_name": task.get("name"),
        "task_modified_at": task.get("modified_at"),
        "story_gid": story.get("gid"),
        "story_created_at": story.get("created_at"),
        "resource_subtype": story.get("resource_subtype"),
        "source": story.get("source"),
        "created_by_gid": (story.get("created_by") or {}).get("gid"),
        "created_by_name": (story.get("created_by") or {}).get("name"),
        "text": story.get("text"),
        "old_name": story.get("old_name"),
        "new_name": story.get("new_name"),
        "old_dates": story.get("old_dates"),
        "new_dates": story.get("new_dates"),
        "old_resource_subtype": story.get("old_resource_subtype"),
        "new_resource_subtype": story.get("new_resource_subtype"),
        "old_approval_status": story.get("old_approval_status"),
        "new_approval_status": story.get("new_approval_status"),
    }


def collect_window_activity(
    client: asana.Client,
    workspace: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> tuple[list[dict], list[dict], int]:
    """Find historical task activity without relying on current modified_at being in-window."""
    # Any task changed during the window must still have current modified_at >= window start.
    # A later repair/edit may move modified_at beyond window end, so modified_at.after is used
    # only to discover candidates. Historical inclusion is decided from story.created_at.
    candidates = client.tasks.search_in_workspace(
        workspace,
        params={
            "modified_at.after": start.isoformat(),
            "sort_by": "modified_at",
            "sort_ascending": True,
        },
        fields=TASK_FIELDS,
        page_size=100,
    )

    task_rows: list[dict] = []
    story_rows: list[dict] = []
    candidate_count = 0

    for task in candidates:
        candidate_count += 1
        matching_stories = [
            story
            for story in client.tasks.stories(
                str(task["gid"]),
                fields=STORY_FIELDS,
                page_size=100,
            )
            if within(story.get("created_at"), start, end)
        ]

        current_modified_in_window = within(task.get("modified_at"), start, end)
        if not matching_stories and not current_modified_in_window:
            continue

        row = dict(task)
        if matching_stories and current_modified_in_window:
            row["window_evidence"] = "story+current_modified_at"
        elif matching_stories:
            row["window_evidence"] = "story"
        else:
            # Keep this fallback because some Asana writes can advance modified_at without
            # yielding a story subtype visible through the stories endpoint.
            row["window_evidence"] = "current_modified_at"
        task_rows.append(row)

        story_rows.extend(make_story_row(task, story) for story in matching_stories)

    return task_rows, story_rows, candidate_count


def main() -> int:
    args = parse_args()
    token = os.environ.get("ASANA_PERSONAL_ACCESS_TOKEN")
    if not token:
        raise SystemExit("ASANA_PERSONAL_ACCESS_TOKEN is not set.")

    start = isoparse(args.start)
    end = isoparse(args.end)
    if start.tzinfo is None or end.tzinfo is None:
        raise SystemExit("--start and --end must include a timezone, preferably Z/UTC.")
    if end < start:
        raise SystemExit("--end must be >= --start.")

    client = asana.Client.access_token(token)
    task_rows, story_rows, candidate_count = collect_window_activity(
        client,
        args.workspace,
        start,
        end,
    )

    prefix = Path(args.output_prefix)
    json_path = prefix.with_suffix(".json")
    tasks_csv_path = prefix.with_name(f"{prefix.name}.tasks.csv")
    stories_csv_path = prefix.with_name(f"{prefix.name}.stories.csv")

    json_path.write_text(
        json.dumps(
            {
                "window": {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "candidate_tasks_modified_since_start": candidate_count,
                },
                "tasks": task_rows,
                "stories": story_rows,
            },
            indent=2,
            ensure_ascii=False,
        ),
    )

    task_columns = [
        "gid",
        "name",
        "modified_at",
        "completed",
        "completed_at",
        "due_on",
        "due_at",
        "window_evidence",
    ]
    with tasks_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=task_columns)
        writer.writeheader()
        writer.writerows(task_rows)

    story_columns = [
        "task_gid",
        "task_name",
        "task_modified_at",
        "story_gid",
        "story_created_at",
        "resource_subtype",
        "source",
        "created_by_gid",
        "created_by_name",
        "text",
        "old_name",
        "new_name",
        "old_dates",
        "new_dates",
        "old_resource_subtype",
        "new_resource_subtype",
        "old_approval_status",
        "new_approval_status",
    ]
    with stories_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=story_columns)
        writer.writeheader()
        for row in story_rows:
            writer.writerow(
                {
                    **row,
                    "old_dates": json.dumps(row["old_dates"], ensure_ascii=False)
                    if row["old_dates"] is not None
                    else "",
                    "new_dates": json.dumps(row["new_dates"], ensure_ascii=False)
                    if row["new_dates"] is not None
                    else "",
                },
            )

    print(f"Candidate tasks modified since window start: {candidate_count}")
    print(f"Tasks with evidence of activity in window: {len(task_rows)}")
    print(f"Stories/events in window: {len(story_rows)}")
    print(f"Wrote: {tasks_csv_path}")
    print(f"Wrote: {stories_csv_path}")
    print(f"Wrote: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
