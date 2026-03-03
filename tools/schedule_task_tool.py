from __future__ import annotations

import os
from typing import Any
from zoneinfo import ZoneInfo

from langchain.tools import tool

from runtime_store import StateStore, format_cz_datetime


def create_schedule_task_tool(store: StateStore) -> Any:
    @tool
    def schedule_task(task_prompt: str, schedule: str, timezone_name: str = "") -> str:
        """Schedule recurring or one-time tasks. Examples: once 2026-02-22 14:30, daily 09:00, weekly mon 14:30, days mon,wed,fri 18:00, every 30 minutes, cron: */10 * * * *."""
        if not timezone_name:
            timezone_name = os.getenv("USER_TIMEZONE", "UTC")
        try:
            ZoneInfo(timezone_name)
        except Exception:
            return f"Invalid timezone '{timezone_name}'. Example: UTC, America/New_York, Europe/London"

        try:
            task = store.add_task(task_prompt=task_prompt, schedule_text=schedule, timezone_name=timezone_name)
            next_run_local = format_cz_datetime(task.get("next_run_utc"), task.get("timezone", "UTC"))
            return (
                f"Scheduled task created. id={task['id']}, schedule='{task['schedule_text']}', "
                f"timezone='{task['timezone']}', next_run={next_run_local}"
            )
        except Exception as exc:
            return f"Could not create schedule: {exc}"

    return schedule_task
