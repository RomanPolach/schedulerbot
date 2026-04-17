from __future__ import annotations

from typing import Any

from langchain.tools import tool

from ..runtime_store import StateStore, format_task_table


def create_list_scheduled_tasks_tool(store: StateStore) -> Any:
    @tool
    def list_scheduled_tasks() -> str:
        """List all scheduled tasks.

        Required args:
        - none

        Returns:
        - one task per line with id, title, status, schedule, next run, timezone, and task prompt.

        Example:
        - list_scheduled_tasks()
        """
        tasks = store.list_tasks()
        return format_task_table(tasks)

    return list_scheduled_tasks
