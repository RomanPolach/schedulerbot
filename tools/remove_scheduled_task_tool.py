from __future__ import annotations

from typing import Any

from langchain.tools import tool

from runtime_store import StateStore


def create_remove_scheduled_task_tool(store: StateStore) -> Any:
    @tool
    def remove_scheduled_task(task_id: str) -> str:
        """Remove a scheduled task by id."""
        removed = store.remove_task(task_id)
        if removed:
            return f"Removed scheduled task {task_id}."
        return f"Task {task_id} not found."

    return remove_scheduled_task

