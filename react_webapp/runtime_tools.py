from __future__ import annotations

from typing import Any, List

from .runtime_store import StateStore
from .tools.list_scheduled_tasks_tool import create_list_scheduled_tasks_tool
from .tools.open_file_tool import create_open_file_tool
from .tools.parse_websites_tool import create_parse_websites_tool
from .tools.parse_websites_tool import warmup_parse_websites_browser
from .tools.remove_scheduled_task_tool import create_remove_scheduled_task_tool
# from tools.run_python_code_tool import create_run_python_code_tool
from .tools.run_windows_cmd_tool import create_run_windows_cmd_tool
from .tools.schedule_task_tool import create_schedule_task_tool
from .tools.web_search_tool import create_web_search_tool


def build_tools(store: StateStore) -> List[Any]:
    # Warm browser once at runtime startup so first parse call is not paying launch cost.
    warmup_status = warmup_parse_websites_browser()
    if warmup_status not in {"ready", "disabled"}:
        print(f"[parse_websites] browser warmup status: {warmup_status}")
    return [
        create_web_search_tool(),
        create_parse_websites_tool(),
        # create_run_python_code_tool(),
        create_open_file_tool(),
        create_run_windows_cmd_tool(),
        create_schedule_task_tool(store),
        create_list_scheduled_tasks_tool(store),
        create_remove_scheduled_task_tool(store),
    ]
