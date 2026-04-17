from __future__ import annotations

import asyncio
import json
import os
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from langchain.agents import create_agent
from langchain_core.messages import SystemMessage

from .runtime_config import (
    AGENT_GRAPH_DEBUG,
    FAILED_TASK_RETRY_SECONDS,
    GEMINI_INCLUDE_THOUGHTS,
    GEMINI_THINKING_BUDGET,
    GEMINI_THINKING_LEVEL,
    MODEL_MAX_RETRIES,
    MODEL_TIMEOUT_SECONDS,
    SCHEDULED_TASK_TIMEOUT_SECONDS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    STATE_FILE,
)
from .runtime_middleware import (
    create_agent_trace_middleware,
    create_tool_call_limit_middleware,
    limit_human_ai_history_middleware,
)
from .runtime_prompt import CHAT_SYSTEM_PROMPT, EXECUTOR_SYSTEM_PROMPT
from .runtime_store import StateStore
from .runtime_tools import build_tools


_AGENT_LOCKS: Dict[int, threading.Lock] = {}
_AGENT_LOCKS_GUARD = threading.Lock()
_RUNTIME_CONTEXT_PREFIX = "USER_TIME:"
_USER_MESSAGE_TYPES = {"HumanMessage", "UserMessage"}
_ASSISTANT_MESSAGE_TYPES = {"AIMessage", "AssistantMessage"}


@dataclass
class AgentRuntime:
    agent: Any
    executor_agent: Any
    store: StateStore
    scheduler: AsyncIOScheduler
    scheduler_loop: asyncio.AbstractEventLoop
    scheduler_thread: threading.Thread


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(content)


def _build_runtime_context_text() -> str:
    def _format_offset_tz_label(dt: datetime) -> str:
        offset = dt.utcoffset()
        if offset is None:
            return "local"
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    now_utc = datetime.now(timezone.utc)
    user_tz_name = (os.getenv("USER_TIMEZONE", "") or "").strip()

    if user_tz_name:
        try:
            from zoneinfo import ZoneInfo

            user_now = now_utc.astimezone(ZoneInfo(user_tz_name))
        except Exception:
            user_now = datetime.now().astimezone()
            user_tz_name = _format_offset_tz_label(user_now)
    else:
        user_now = datetime.now().astimezone()
        user_tz_name = _format_offset_tz_label(user_now)

    formatted_user_time = (
        f"{user_now.day}. {user_now.month}. {user_now.year} - {user_now.strftime('%H:%M')}"
    )
    return f"{_RUNTIME_CONTEXT_PREFIX} {formatted_user_time} ({user_tz_name})"


def _with_runtime_context(messages: List[Any]) -> List[Any]:
    return [SystemMessage(content=_build_runtime_context_text()), *list(messages)]


def _strip_runtime_context_messages(messages: List[Any]) -> List[Any]:
    stripped: List[Any] = []
    for message in messages:
        msg_type_name = type(message).__name__
        if msg_type_name == "SystemMessage":
            text = content_to_text(getattr(message, "content", ""))
            if text.startswith(_RUNTIME_CONTEXT_PREFIX):
                continue
        stripped.append(message)
    return stripped


def is_transient_failure_response(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered.strip():
        return True
    failure_markers = [
        "scheduled execution failed",
        "scheduled execution timed out",
        "web_search failed",
        "connection error",
        "max retries exceeded",
        "failed to resolve",
        "name resolution",
        "temporarily unavailable",
        "service unavailable",
        "request timed out",
        "read timed out",
        "network is unreachable",
    ]
    return any(marker in lowered for marker in failure_markers)


def _get_agent_lock(agent: Any) -> threading.Lock:
    key = id(agent)
    with _AGENT_LOCKS_GUARD:
        lock = _AGENT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _AGENT_LOCKS[key] = lock
    return lock


def _start_scheduler_event_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run_loop, name="scheduler-event-loop", daemon=True)
    thread.start()
    return loop, thread


def _create_gemini_model(model_name: str) -> Any:
    google_api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not google_api_key:
        raise RuntimeError("Missing API key. Set GOOGLE_API_KEY (or GEMINI_API_KEY).")
    # Prefer a single explicit key source to avoid noisy dual-key warnings.
    os.environ.pop("GEMINI_API_KEY", None)
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except Exception as exc:
        raise RuntimeError("Missing dependency for Gemini. Install `langchain-google-genai`.") from exc
    model_temperature = 1.0
    model_timeout = max(10.0, min(float(os.getenv("MODEL_TIMEOUT_SECONDS", str(MODEL_TIMEOUT_SECONDS))), 300.0))
    model_retries = max(0, min(int(os.getenv("MODEL_MAX_RETRIES", str(MODEL_MAX_RETRIES))), 5))
    return ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=google_api_key,
        temperature=model_temperature,
        timeout=model_timeout,
        max_retries=model_retries,
        include_thoughts=GEMINI_INCLUDE_THOUGHTS,
        thinking_level=GEMINI_THINKING_LEVEL,
        thinking_budget=GEMINI_THINKING_BUDGET,
    )


def _create_openrouter_model(model_name: str) -> Any:
    openrouter_api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not openrouter_api_key:
        raise RuntimeError("Missing API key. Set OPENROUTER_API_KEY.")
    try:
        from langchain_openai import ChatOpenAI
    except Exception as exc:
        raise RuntimeError("Missing dependency for OpenRouter compatibility mode. Install `langchain-openai`.") from exc
    model_temperature = 1.0
    model_timeout = max(10.0, min(float(os.getenv("MODEL_TIMEOUT_SECONDS", str(MODEL_TIMEOUT_SECONDS))), 300.0))
    model_retries = max(0, min(int(os.getenv("MODEL_MAX_RETRIES", str(MODEL_MAX_RETRIES))), 5))
    return ChatOpenAI(
        model=model_name,
        api_key=openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=model_temperature,
        timeout=model_timeout,
        max_retries=model_retries,
        extra_body={
            "provider": {
                "only": ["fireworks"],
                "allow_fallbacks": False,
            }
        },
    )


def _emit_scheduled_task_event(
    store: StateStore,
    task_id: str,
    task_title: str,
    message: str,
    tool_calls: List[Dict[str, Any]],
    status: str,
) -> None:
    store.add_event(
        {
            "type": "scheduled_task_result",
            "task_id": task_id,
            "task_title": task_title,
            "status": status,
            "message": message,
            "tool_calls": tool_calls,
        }
    )


async def _run_single_due_task_async(
    agent: Any,
    store: StateStore,
    task_id: str,
    task_prompt: str,
    task_type: str = "recurring",
    task_title: str = "",
) -> None:
    now_utc = datetime.now(timezone.utc)
    should_retry = False
    is_once_task = (task_type or "").lower() == "once"
    tool_calls: List[Dict[str, Any]] = []
    clean_task_title = " ".join(str(task_title or "").split()).strip()
    if not clean_task_title:
        try:
            task = next((item for item in store.list_tasks() if item.get("id") == task_id), None)
            raw_title = " ".join(str((task or {}).get("title", "")).split()).strip()
            if raw_title:
                clean_task_title = raw_title
        except Exception:
            clean_task_title = ""
    clean_task_title = store.resolve_task_title(task_id, fallback=clean_task_title or task_id)
    try:
        detailed = await asyncio.wait_for(
            invoke_agent_async_detailed(
                agent,
                [
                    {
                        "role": "user",
                        "content": (
                            "Scheduled task execution phase (existing task). "
                            "Execute the task now and return the result. "
                            "Do not create, edit, remove, or list schedules in this phase. "
                            f"Task id: {task_id}. "
                            f"Instructions: {task_prompt}"
                        ),
                    }
                ],
            ),
            timeout=SCHEDULED_TASK_TIMEOUT_SECONDS,
        )
        response = detailed["text"]
        tool_calls = detailed.get("tool_calls", [])
        should_retry = is_transient_failure_response(response)
    except asyncio.TimeoutError:
        response = (
            "Scheduled execution timed out before completion. "
            "The task will run again on the next schedule."
        )
        should_retry = True
    except Exception:
        response = f"Scheduled execution failed:\n{traceback.format_exc(limit=2)}"
        should_retry = True

    try:
        # One-time tasks are at-most-once by design: never schedule retries.
        if is_once_task:
            if should_retry:
                response = (
                    f"{response}\n\n"
                    "One-time task will not be retried automatically."
                )
            _emit_scheduled_task_event(
                store,
                task_id,
                clean_task_title,
                response,
                tool_calls,
                status="failed_no_retry" if should_retry else "completed",
            )
            return

        if should_retry:
            store.mark_task_retry(
                task_id=task_id,
                result=response,
                now_utc=now_utc,
                retry_after_seconds=FAILED_TASK_RETRY_SECONDS,
            )
            retry_msg = (
                f"{response}\n\n"
                f"Retry scheduled in {FAILED_TASK_RETRY_SECONDS} seconds because this run failed."
            )
            _emit_scheduled_task_event(
                store,
                task_id,
                clean_task_title,
                retry_msg,
                tool_calls,
                status="retry_scheduled",
            )
            return

        store.mark_task_run(task_id=task_id, result=response, now_utc=now_utc)
        _emit_scheduled_task_event(
            store,
            task_id,
            clean_task_title,
            response,
            tool_calls,
            status="completed",
        )
    except Exception:
        # Keep scheduler loop resilient even if state write fails unexpectedly.
        traceback.print_exc(limit=2)


def create_runtime(model_name: str) -> AgentRuntime:
    store = StateStore(STATE_FILE)
    tools = build_tools(store)
    executor_tools = [
        tool
        for tool in tools
        if getattr(tool, "name", "")
        not in {"schedule_task", "list_scheduled_tasks", "remove_scheduled_task"}
    ]

    llm = _create_gemini_model(model_name)
    executor_llm = _create_gemini_model(model_name)
    # llm = _create_openrouter_model(model_name)
    # executor_llm = _create_openrouter_model(model_name)

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=CHAT_SYSTEM_PROMPT,
        middleware=[
            limit_human_ai_history_middleware,
            create_tool_call_limit_middleware(),
            create_agent_trace_middleware("chat-agent"),
        ],
        debug=AGENT_GRAPH_DEBUG,
        name="chat-agent",
    )
    executor_agent = create_agent(
        model=executor_llm,
        tools=executor_tools,
        system_prompt=EXECUTOR_SYSTEM_PROMPT,
        middleware=[
            create_tool_call_limit_middleware(),
            create_agent_trace_middleware("executor-agent"),
        ],
        debug=AGENT_GRAPH_DEBUG,
        name="executor-agent",
    )

    scheduler_loop, scheduler_thread = _start_scheduler_event_loop()
    poll_seconds = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))
    poll_seconds = max(10, min(poll_seconds, 3600))
    scheduler = AsyncIOScheduler(timezone="UTC", event_loop=scheduler_loop)
    scheduler.add_job(
        run_due_tasks_async,
        "interval",
        seconds=poll_seconds,
        kwargs={"agent": executor_agent, "store": store},
        id="due-task-runner",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=SCHEDULER_MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(
        run_due_tasks_async,
        "date",
        run_date=datetime.now(timezone.utc),
        kwargs={"agent": executor_agent, "store": store},
        id="due-task-startup-runner",
        replace_existing=True,
        misfire_grace_time=SCHEDULER_MISFIRE_GRACE_SECONDS,
    )
    scheduler.start()

    return AgentRuntime(
        agent=agent,
        executor_agent=executor_agent,
        store=store,
        scheduler=scheduler,
        scheduler_loop=scheduler_loop,
        scheduler_thread=scheduler_thread,
    )


async def run_due_tasks_async(agent: Any, store: StateStore) -> None:
    now_utc = datetime.now(timezone.utc)
    due_tasks = store.get_due_tasks(now_utc)
    for task in due_tasks:
        task_id = task["id"]
        task_prompt = task["task_prompt"]
        task_type = task.get("task_type", "recurring")
        task_title = " ".join(str(task.get("title", "")).split()).strip() or task_id
        if (task_type or "").lower() == "once":
            # Persist one-time consumption before execution. This prevents stale tasks
            # when commands terminate the process (e.g., shutdown/restart).
            store.remove_task(task_id)
        await _run_single_due_task_async(
            agent,
            store,
            task_id,
            task_prompt,
            task_type,
            task_title,
        )


def _extract_agent_text(result: Dict[str, Any]) -> str:
    output_messages = result.get("messages", [])
    if not output_messages:
        return ""

    for message in reversed(output_messages):
        msg_type_name = type(message).__name__
        if msg_type_name not in _ASSISTANT_MESSAGE_TYPES:
            continue
        text = content_to_text(getattr(message, "content", message))
        if text.strip():
            return text

    final = output_messages[-1]
    return content_to_text(getattr(final, "content", final))


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def _messages_after_latest_user(messages: List[Any]) -> List[Any]:
    """Return only messages belonging to the latest user turn."""
    if not messages:
        return []
    last_user_index = -1
    for idx, message in enumerate(messages):
        if type(message).__name__ in _USER_MESSAGE_TYPES:
            last_user_index = idx
    if last_user_index < 0:
        return list(messages)
    return list(messages[last_user_index + 1 :])


def _extract_tool_calls_from_messages(messages: List[Any]) -> List[Dict[str, Any]]:
    turn_messages = _messages_after_latest_user(messages)
    calls: List[Dict[str, Any]] = []
    for message in turn_messages:
        tool_calls = getattr(message, "tool_calls", None)
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            if not name:
                continue
            calls.append(
                {
                    "name": str(name),
                    "args": _json_safe(call.get("args", {})),
                }
            )
    return calls


def _extract_tool_calls(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _extract_tool_calls_from_messages(result.get("messages", []))


def _build_invoke_details(result: Dict[str, Any]) -> Dict[str, Any]:
    cleaned_messages = _strip_runtime_context_messages(result.get("messages", []))
    cleaned_result = {"messages": cleaned_messages}
    tool_calls = _extract_tool_calls(cleaned_result)
    return {
        "text": _extract_agent_text(cleaned_result),
        "tool_calls": tool_calls,
        "messages": cleaned_messages,
        "result": result,
    }


def invoke_agent_detailed(agent: Any, messages: List[Any]) -> Dict[str, Any]:
    with _get_agent_lock(agent):
        result = agent.invoke({"messages": _with_runtime_context(messages)})
    return _build_invoke_details(result)


def invoke_agent_streaming_detailed(
    agent: Any,
    messages: List[Any],
    on_text: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    latest_values: Dict[str, Any] | None = None
    active_message_id = ""
    active_text = ""

    with _get_agent_lock(agent):
        for mode, payload in agent.stream(
            {"messages": _with_runtime_context(messages)},
            stream_mode=["messages", "values"],
        ):
            if mode == "values":
                if isinstance(payload, dict):
                    latest_values = payload
                continue

            if mode != "messages" or not isinstance(payload, tuple) or len(payload) != 2:
                continue

            chunk, metadata = payload
            if not isinstance(metadata, dict) or metadata.get("langgraph_node") != "model":
                continue

            chunk_id = str(getattr(chunk, "id", "") or "")
            if chunk_id and chunk_id != active_message_id:
                active_message_id = chunk_id
                active_text = ""

            delta_text = content_to_text(getattr(chunk, "content", ""))
            if not delta_text:
                continue

            active_text += delta_text
            if on_text is not None:
                on_text(active_text)

    if latest_values is None:
        raise RuntimeError("Agent stream completed without a final state payload.")

    detailed = _build_invoke_details(latest_values)
    final_text = detailed["text"]
    if on_text is not None and final_text and final_text != active_text:
        on_text(final_text)
    return detailed


def invoke_agent(agent: Any, messages: List[Any]) -> str:
    return invoke_agent_detailed(agent, messages)["text"]


async def invoke_agent_async(agent: Any, messages: List[Any]) -> str:
    return (await invoke_agent_async_detailed(agent, messages))["text"]


async def invoke_agent_async_detailed(agent: Any, messages: List[Any]) -> Dict[str, Any]:
    # Keep async scheduler flow non-blocking while using sync invoke underneath.
    # This avoids provider/tool stacks that fail in ainvoke() contexts.
    return await asyncio.to_thread(invoke_agent_detailed, agent, messages)


def trigger_task_now(
    executor_agent: Any,
    store: StateStore,
    scheduler_loop: asyncio.AbstractEventLoop,
    task_id: str,
) -> Dict[str, Any]:
    tasks = store.list_tasks()
    task = next((item for item in tasks if item.get("id") == task_id), None)
    if not task:
        return {"started": False, "task_id": task_id, "message": f"Task {task_id} not found."}

    task_title = " ".join(str(task.get("title", "")).split()).strip() or task_id

    task_prompt = str(task.get("task_prompt", "")).strip()
    if not task_prompt:
        return {
            "started": False,
            "task_id": task_id,
            "task_title": task_title,
            "message": f'Task "{task_title}" has no text, so it was not started.',
        }

    task_type = str(task.get("task_type", "recurring") or "recurring")
    if task_type.lower() == "once":
        # Keep one-time semantics consistent with scheduled execution path.
        store.remove_task(task_id)

    future = asyncio.run_coroutine_threadsafe(
        _run_single_due_task_async(
            agent=executor_agent,
            store=store,
            task_id=task_id,
            task_prompt=task_prompt,
            task_type=task_type,
            task_title=task_title,
        ),
        scheduler_loop,
    )
    if future.cancelled():
        return {
            "started": False,
            "task_id": task_id,
            "task_title": task_title,
            "message": f'Running task "{task_title}" manually was cancelled before it started.',
        }
    return {
        "started": True,
        "task_id": task_id,
        "task_title": task_title,
        "message": f'Running task "{task_title}" manually...',
    }
