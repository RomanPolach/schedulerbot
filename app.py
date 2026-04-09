import os

import streamlit as st
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage
from streamlit_autorefresh import st_autorefresh

from runtime import create_runtime, invoke_agent_streaming_detailed
from ui.chat_ui import (
    is_legacy_intro_message,
    is_scheduled_result_message,
    render_manual_run_statuses,
    render_page_header,
    render_scheduled_result_message,
    render_tool_calls_panel,
    should_render_tool_calls,
    to_agent_messages,
)
from ui.styles import apply_app_styles
from ui.task_ui import (
    classify_task_bucket,
    handle_task_card_action,
    render_task_card,
    send_windows_os_alert,
)


load_dotenv()

MODEL_NAME = os.getenv("AGENT_MODEL", "gemini-3-flash-preview")
AUTO_REFRESH_MS = int(os.getenv("AUTO_REFRESH_MS", "30000"))
MAX_CHAT_MESSAGES = max(50, min(int(os.getenv("MAX_CHAT_MESSAGES", "300")), 5000))
MAX_AGENT_MESSAGES = max(50, min(int(os.getenv("MAX_AGENT_MESSAGES", "180")), 8000))
WINDOWS_OS_ALERTS = os.getenv("WINDOWS_OS_ALERTS", "true").strip().lower() in {"1", "true", "yes", "on"}
WINDOWS_OS_ALERT_TIMEOUT_SECONDS = max(1, min(int(os.getenv("WINDOWS_OS_ALERT_TIMEOUT_SECONDS", "1800")), 86400))
RUNTIME_SCHEMA_VERSION = "2026-03-04-task-title-required-v1"
EMPTY_CHAT_SUBTITLE = "Ask anything or set up a recurring task."
LEGACY_INTRO_MESSAGE = "How can I help? I can answer directly or schedule recurring tasks."

st.set_page_config(page_title="Schedule Chatbot", page_icon="chat", layout="wide")


@st.cache_resource
def get_runtime(runtime_schema_version: str):
    _ = runtime_schema_version
    return create_runtime(model_name=MODEL_NAME)


runtime = get_runtime(RUNTIME_SCHEMA_VERSION)
apply_app_styles()


def trim_session_histories() -> None:
    st.session_state.messages = st.session_state.messages[-MAX_CHAT_MESSAGES:]
    st.session_state.agent_messages = st.session_state.agent_messages[-MAX_AGENT_MESSAGES:]


def append_message(
    message: dict,
    *,
    persist: bool = True,
    add_to_agent_history: bool = True,
    extra_fields: dict | None = None,
) -> None:
    st.session_state.messages.append(message)

    if add_to_agent_history:
        if message.get("role") == "user":
            st.session_state.agent_messages.append(HumanMessage(content=message.get("content", "")))
        elif message.get("role") == "assistant":
            st.session_state.agent_messages.append(AIMessage(content=message.get("content", "")))

    if persist:
        runtime.store.append_chat_message(
            role=message.get("role", ""),
            content=message.get("content", ""),
            tool_calls=message.get("tool_calls"),
            message_type=message.get("message_type"),
            extra_fields=extra_fields,
        )


def task_sort_key(task: dict) -> str:
    return task.get("next_run_utc") or "9999-12-31T23:59:59+00:00"


if "messages" not in st.session_state:
    persisted_messages = [
        message
        for message in runtime.store.get_chat_messages()
        if not is_legacy_intro_message(message, LEGACY_INTRO_MESSAGE)
    ]
    st.session_state.messages = persisted_messages or []

if "agent_messages" not in st.session_state:
    st.session_state.agent_messages = to_agent_messages(st.session_state.messages)

trim_session_histories()

if "last_event_id" not in st.session_state:
    st.session_state.last_event_id = runtime.store.get_processed_event_id()

if "unread_scheduled_result_ids" not in st.session_state:
    st.session_state.unread_scheduled_result_ids = set()

if "manual_run_statuses" not in st.session_state:
    st.session_state.manual_run_statuses = {}


header_placeholder = st.empty()
render_page_header(header_placeholder, EMPTY_CHAT_SUBTITLE, bool(st.session_state.messages))

with st.sidebar:
    with st.container(border=False, key="clear_conversation_shell"):
        button_col, _ = st.columns([0.85, 2.15])
        with button_col:
            if st.button("Clear chat", key="clear_conversation_button", type="secondary"):
                runtime.store.clear_chat_messages()
                st.session_state.messages = []
                st.session_state.agent_messages = []
                st.rerun()

    st.subheader("Scheduled Tasks")

    tasks = runtime.store.list_tasks()
    if tasks:
        grouped_tasks = {"today": [], "tomorrow": [], "future": []}
        for task in sorted(tasks, key=task_sort_key):
            grouped_tasks[classify_task_bucket(task)].append(task)

        for group_key, group_label, expanded in [
            ("today", "Today's Tasks", True),
            ("tomorrow", "Tomorrow's Tasks", False),
            ("future", "Future Tasks", False),
        ]:
            group_tasks = grouped_tasks[group_key]
            if not group_tasks:
                continue

            with st.container(border=False, key=f"taskgroup_{group_key}"):
                with st.expander(f"{group_label} ({len(group_tasks)})", expanded=expanded):
                    for task in group_tasks:
                        action = render_task_card(task)
                        if action:
                            handle_task_card_action(action, runtime, append_message, trim_session_histories)
    else:
        st.info("No scheduled tasks.")

should_auto_refresh = bool(tasks or st.session_state.manual_run_statuses)


new_events = runtime.store.get_events_after(st.session_state.last_event_id)
for event in new_events:
    if event.get("type") != "scheduled_task_result":
        continue

    scheduled_event_id = str(event.get("id", ""))
    result_text = str(event.get("message", "") or "")
    task_title = " ".join(str(event.get("task_title", "")).split()).strip()
    scheduled_message = {
        "role": "assistant",
        "content": result_text,
        "tool_calls": event.get("tool_calls", []),
        "message_type": "scheduled",
        "scheduled_event_id": scheduled_event_id,
        "scheduled_created_at": event.get("created_at"),
        "scheduled_status": event.get("status", "completed"),
        "task_id": event.get("task_id"),
        "task_title": task_title or event.get("task_id"),
    }
    append_message(
        scheduled_message,
        extra_fields={
            "scheduled_event_id": scheduled_message["scheduled_event_id"],
            "scheduled_created_at": scheduled_message["scheduled_created_at"],
            "scheduled_status": scheduled_message["scheduled_status"],
            "task_id": scheduled_message["task_id"],
            "task_title": scheduled_message["task_title"],
        },
    )
    st.session_state.manual_run_statuses.pop(str(event.get("task_id", "")), None)
    st.session_state.unread_scheduled_result_ids.add(scheduled_event_id)
    send_windows_os_alert(
        f"Scheduled task {event.get('task_id')} completed. Open Schedule Assistant.",
        enabled=os.name == "nt" and WINDOWS_OS_ALERTS,
        timeout_seconds=WINDOWS_OS_ALERT_TIMEOUT_SECONDS,
    )
    trim_session_histories()

if new_events:
    latest_event_id = new_events[-1]["id"]
    st.session_state.last_event_id = latest_event_id
    runtime.store.set_processed_event_id(latest_event_id)


render_manual_run_statuses(st.session_state.manual_run_statuses)


for index, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        if is_scheduled_result_message(message):
            render_scheduled_result_message(message, index)
        else:
            st.markdown(message["content"])
            if should_render_tool_calls(message):
                render_tool_calls_panel(message.get("tool_calls"))


prompt = st.chat_input("Message...")
if prompt:
    append_message({"role": "user", "content": prompt})

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        response_placeholder = st.empty()
        response_placeholder.markdown("_Working..._")
        try:
            detailed = invoke_agent_streaming_detailed(
                runtime.agent,
                st.session_state.agent_messages,
                on_text=response_placeholder.markdown,
            )
            response = detailed["text"]
            response_tool_calls = detailed.get("tool_calls", [])
            st.session_state.agent_messages = detailed["messages"]
        except Exception as exc:
            response = f"Agent error: {exc}"
            response_tool_calls = []

        response_placeholder.markdown(response)
        render_tool_calls_panel(response_tool_calls)

    append_message(
        {
            "role": "assistant",
            "content": response,
            "tool_calls": response_tool_calls,
            "message_type": "ai",
        },
        add_to_agent_history=False,
    )
    trim_session_histories()
    st.rerun()

render_page_header(header_placeholder, EMPTY_CHAT_SUBTITLE, bool(st.session_state.messages))
if should_auto_refresh:
    st_autorefresh(interval=AUTO_REFRESH_MS, key="chat_auto_refresh")
