from __future__ import annotations

import os
from typing import Any
from zoneinfo import ZoneInfo

from langchain.tools import tool

from ..runtime_store import StateStore, format_cz_datetime


def create_schedule_task_tool(store: StateStore) -> Any:
    @tool
    def schedule_task(title: str, task_prompt: str, schedule: str, timezone_name: str = "") -> str:
        """Create a scheduled task.

        Required args:
        - title: short task name shown in task cards.
        - task_prompt: what to execute when the task runs.
        - schedule: one of supported schedule formats.
        Optional args:
        - timezone_name: IANA timezone (defaults to USER_TIMEZONE or UTC).

        Returns:
        - creation status with task id, title, schedule, timezone, and next run timestamp.

        Valid schedule formats:
        - once YYYY-MM-DD HH:MM
        - daily HH:MM
        - weekly mon HH:MM
        - days mon,wed,fri HH:MM
        - every N minutes
        - cron: M H DOM MON DOW

        Examples:
        - title="Morning AI News", task_prompt="Summarize top AI headlines with links.", schedule="daily 09:00", timezone_name="Europe/Prague"
        - title="One-time Release Check", task_prompt="Check latest Android release notes and summarize.", schedule="once 2026-03-06 14:30", timezone_name="Europe/Prague"
        - title="Frequent Web Digest", task_prompt="Parse cnn.com and bbc.com and provide a short digest.", schedule="every 30 minutes", timezone_name="UTC"
        """
        title = " ".join((title or "").split()).strip()
        if not title:
            return "Task title is required and cannot be empty."

        if not timezone_name:
            timezone_name = os.getenv("USER_TIMEZONE", "UTC")
        try:
            ZoneInfo(timezone_name)
        except Exception:
            return f"Invalid timezone '{timezone_name}'. Example: UTC, America/New_York, Europe/London"

        try:
            task = store.add_task(
                title=title,
                task_prompt=task_prompt,
                schedule_text=schedule,
                timezone_name=timezone_name,
            )
            next_run_local = format_cz_datetime(task.get("next_run_utc"), task.get("timezone", "UTC"))
            return (
                f"Scheduled task created. id={task['id']}, title='{task['title']}', schedule='{task['schedule_text']}', "
                f"timezone='{task['timezone']}', next_run={next_run_local}"
            )
        except Exception as exc:
            return f"Could not create schedule: {exc}"

    return schedule_task
