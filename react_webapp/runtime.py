from __future__ import annotations

# Compatibility facade: keep legacy imports stable while code lives in focused modules.
from .runtime_agent import (
    AgentRuntime,
    content_to_text,
    create_runtime,
    invoke_agent,
    invoke_agent_async,
    invoke_agent_detailed,
    invoke_agent_streaming_detailed,
    is_transient_failure_response,
    run_due_tasks_async,
    trigger_task_now,
)
from .runtime_config import (
    FAILED_TASK_RETRY_SECONDS,
    MAX_CHAT_MESSAGES,
    MAX_SITE_CONTENT_CHARS,
    MODEL_MAX_RETRIES,
    MODEL_PROVIDER,
    MODEL_TEMPERATURE,
    MODEL_TIMEOUT_SECONDS,
    SCHEDULED_TASK_TIMEOUT_SECONDS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SEARCH_MAX_RETRIES,
    SEARCH_TIMEOUT_SECONDS,
    STATE_FILE,
)
from .runtime_prompt import CHAT_SYSTEM_PROMPT, EXECUTOR_SYSTEM_PROMPT, SYSTEM_PROMPT
from .runtime_schedule import (
    DAY_TO_CRON,
    get_next_run_utc,
    parse_hhmm,
    parse_schedule_definition,
    parse_schedule_to_cron,
)
from .runtime_store import StateStore, dt_to_iso, format_task_table, iso_to_dt, utc_now_iso
from .runtime_tools import build_tools


__all__ = [
    "AgentRuntime",
    "DAY_TO_CRON",
    "FAILED_TASK_RETRY_SECONDS",
    "MAX_CHAT_MESSAGES",
    "MAX_SITE_CONTENT_CHARS",
    "MODEL_MAX_RETRIES",
    "MODEL_PROVIDER",
    "MODEL_TEMPERATURE",
    "MODEL_TIMEOUT_SECONDS",
    "SCHEDULED_TASK_TIMEOUT_SECONDS",
    "SCHEDULER_MISFIRE_GRACE_SECONDS",
    "SEARCH_MAX_RETRIES",
    "SEARCH_TIMEOUT_SECONDS",
    "STATE_FILE",
    "CHAT_SYSTEM_PROMPT",
    "EXECUTOR_SYSTEM_PROMPT",
    "SYSTEM_PROMPT",
    "StateStore",
    "build_tools",
    "content_to_text",
    "create_runtime",
    "dt_to_iso",
    "format_task_table",
    "get_next_run_utc",
    "invoke_agent",
    "invoke_agent_async",
    "invoke_agent_detailed",
    "invoke_agent_streaming_detailed",
    "is_transient_failure_response",
    "iso_to_dt",
    "parse_hhmm",
    "parse_schedule_definition",
    "parse_schedule_to_cron",
    "run_due_tasks_async",
    "trigger_task_now",
    "utc_now_iso",
]
