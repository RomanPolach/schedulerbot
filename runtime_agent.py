from __future__ import annotations

import asyncio
import json
import os
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from langchain.agents import create_agent
from langchain_core.messages import SystemMessage
from langchain_xai import ChatXAI

from runtime_config import (
    FAILED_TASK_RETRY_SECONDS,
    MODEL_MAX_RETRIES,
    MODEL_TEMPERATURE,
    MODEL_TIMEOUT_SECONDS,
    SCHEDULED_TASK_TIMEOUT_SECONDS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    STATE_FILE,
)
from runtime_middleware import limit_human_ai_history_middleware
from runtime_prompt import SYSTEM_PROMPT
from runtime_store import StateStore
from runtime_tools import build_tools


_AGENT_LOCKS: Dict[int, threading.Lock] = {}
_AGENT_LOCKS_GUARD = threading.Lock()
_RUNTIME_CONTEXT_PREFIX = "RUNTIME_CONTEXT:"
_USER_MESSAGE_TYPES = {"HumanMessage", "UserMessage"}
_ASSISTANT_MESSAGE_TYPES = {"AIMessage", "AssistantMessage"}


@dataclass
class AgentRuntime:
    agent: Any
    scheduler_agent: Any
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
    now_utc = datetime.now(timezone.utc)
    user_tz_name = (os.getenv("USER_TIMEZONE", "") or "").strip()

    if user_tz_name:
        try:
            from zoneinfo import ZoneInfo

            user_now = now_utc.astimezone(ZoneInfo(user_tz_name))
        except Exception:
            user_now = datetime.now().astimezone()
            user_tz_name = str(user_now.tzinfo or "local")
    else:
        user_now = datetime.now().astimezone()
        user_tz_name = str(user_now.tzinfo or "local")

    return (
        f"{_RUNTIME_CONTEXT_PREFIX}\n"
        f"Current UTC time: {now_utc.isoformat()}\n"
        f"User timezone: {user_tz_name}\n"
        f"Current user-local time: {user_now.isoformat()}"
    )


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


def _create_xai_model(model_name: str, xai_api_key: str, xai_base_url: str) -> ChatXAI:
    model_temperature = max(0.0, min(float(os.getenv("MODEL_TEMPERATURE", str(MODEL_TEMPERATURE))), 2.0))
    model_timeout = max(10.0, min(float(os.getenv("MODEL_TIMEOUT_SECONDS", str(MODEL_TIMEOUT_SECONDS))), 300.0))
    model_retries = max(0, min(int(os.getenv("MODEL_MAX_RETRIES", str(MODEL_MAX_RETRIES))), 5))
    return ChatXAI(
        model=model_name,
        api_key=xai_api_key,
        xai_api_base=xai_base_url,
        temperature=model_temperature,
        timeout=model_timeout,
        max_retries=model_retries,
        use_responses_api=True,
    )


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
    model_temperature = max(0.0, min(float(os.getenv("MODEL_TEMPERATURE", str(MODEL_TEMPERATURE))), 2.0))
    model_timeout = max(10.0, min(float(os.getenv("MODEL_TIMEOUT_SECONDS", str(MODEL_TIMEOUT_SECONDS))), 300.0))
    model_retries = max(0, min(int(os.getenv("MODEL_MAX_RETRIES", str(MODEL_MAX_RETRIES))), 5))
    return ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=google_api_key,
        temperature=model_temperature,
        timeout=model_timeout,
        max_retries=model_retries,
    )


def _emit_scheduled_task_event(
    store: StateStore,
    task_id: str,
    message: str,
    tool_calls: List[Dict[str, Any]],
) -> None:
    store.add_event(
        {
            "type": "scheduled_task_result",
            "task_id": task_id,
            "message": message,
            "tool_calls": tool_calls,
        }
    )


async def _run_single_due_task_async(
    agent: Any, store: StateStore, task_id: str, task_prompt: str, task_type: str = "recurring"
) -> None:
    now_utc = datetime.now(timezone.utc)
    should_retry = False
    is_once_task = (task_type or "").lower() == "once"
    tool_calls: List[Dict[str, Any]] = []
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
            _emit_scheduled_task_event(store, task_id, response, tool_calls)
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
            _emit_scheduled_task_event(store, task_id, retry_msg, tool_calls)
            return

        store.mark_task_run(task_id=task_id, result=response, now_utc=now_utc)
        _emit_scheduled_task_event(store, task_id, response, tool_calls)
    except Exception:
        # Keep scheduler loop resilient even if state write fails unexpectedly.
        traceback.print_exc(limit=2)


def create_runtime(model_name: str) -> AgentRuntime:
    store = StateStore(STATE_FILE)
    tools = build_tools(store)
    scheduler_tools = [
        tool
        for tool in tools
        if getattr(tool, "name", "")
        not in {"schedule_task", "list_scheduled_tasks", "remove_scheduled_task"}
    ]

    provider = (os.getenv("MODEL_PROVIDER", "xai") or "").strip().lower()
    if provider in {"xai", "grok"}:
        xai_api_key = os.getenv("XAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not xai_api_key:
            raise RuntimeError("Missing API key. Set XAI_API_KEY (or OPENAI_API_KEY).")
        xai_base_url = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
        llm = _create_xai_model(model_name, xai_api_key, xai_base_url)
        scheduler_llm = _create_xai_model(model_name, xai_api_key, xai_base_url)
    elif provider in {"gemini", "google"}:
        llm = _create_gemini_model(model_name)
        scheduler_llm = _create_gemini_model(model_name)
    else:
        raise RuntimeError("Unsupported MODEL_PROVIDER. Use 'xai' or 'gemini'.")

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=[limit_human_ai_history_middleware],
    )
    scheduler_agent = create_agent(
        model=scheduler_llm,
        tools=scheduler_tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=[limit_human_ai_history_middleware],
    )

    scheduler_loop, scheduler_thread = _start_scheduler_event_loop()
    poll_seconds = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))
    poll_seconds = max(10, min(poll_seconds, 3600))
    scheduler = AsyncIOScheduler(timezone="UTC", event_loop=scheduler_loop)
    scheduler.add_job(
        run_due_tasks_async,
        "interval",
        seconds=poll_seconds,
        kwargs={"agent": scheduler_agent, "store": store},
        id="due-task-runner",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=SCHEDULER_MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(
        run_due_tasks_async,
        "date",
        run_date=datetime.now(timezone.utc),
        kwargs={"agent": scheduler_agent, "store": store},
        id="due-task-startup-runner",
        replace_existing=True,
        misfire_grace_time=SCHEDULER_MISFIRE_GRACE_SECONDS,
    )
    scheduler.start()

    return AgentRuntime(
        agent=agent,
        scheduler_agent=scheduler_agent,
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
        if (task_type or "").lower() == "once":
            # Persist one-time consumption before execution. This prevents stale tasks
            # when commands terminate the process (e.g., shutdown/restart).
            store.remove_task(task_id)
        await _run_single_due_task_async(agent, store, task_id, task_prompt, task_type)


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


def invoke_agent_detailed(agent: Any, messages: List[Dict[str, str]]) -> Dict[str, Any]:
    with _get_agent_lock(agent):
        result = agent.invoke({"messages": _with_runtime_context(messages)})
    return _build_invoke_details(result)


def invoke_agent(agent: Any, messages: List[Dict[str, str]]) -> str:
    return invoke_agent_detailed(agent, messages)["text"]


async def invoke_agent_async(agent: Any, messages: List[Dict[str, str]]) -> str:
    return (await invoke_agent_async_detailed(agent, messages))["text"]


async def invoke_agent_async_detailed(agent: Any, messages: List[Dict[str, str]]) -> Dict[str, Any]:
    result = await agent.ainvoke({"messages": _with_runtime_context(messages)})
    return _build_invoke_details(result)


def trigger_task_now(
    scheduler_agent: Any,
    store: StateStore,
    scheduler_loop: asyncio.AbstractEventLoop,
    task_id: str,
) -> str:
    tasks = store.list_tasks()
    task = next((item for item in tasks if item.get("id") == task_id), None)
    if not task:
        return f"Task {task_id} not found."

    task_prompt = str(task.get("task_prompt", "")).strip()
    if not task_prompt:
        return f"Task {task_id} has empty task prompt; run was not started."

    task_type = str(task.get("task_type", "recurring") or "recurring")
    if task_type.lower() == "once":
        # Keep one-time semantics consistent with scheduled execution path.
        store.remove_task(task_id)

    future = asyncio.run_coroutine_threadsafe(
        _run_single_due_task_async(
            agent=scheduler_agent,
            store=store,
            task_id=task_id,
            task_prompt=task_prompt,
            task_type=task_type,
        ),
        scheduler_loop,
    )
    if future.cancelled():
        return f"Manual run for task {task_id} was cancelled before start."
    return f"Manual run started for task {task_id}."
