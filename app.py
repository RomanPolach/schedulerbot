import os
import json
import subprocess
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage
from streamlit_autorefresh import st_autorefresh

from runtime import create_runtime, invoke_agent_detailed, trigger_task_now
from runtime_store import format_cz_datetime


load_dotenv()

MODEL_NAME = os.getenv("AGENT_MODEL", "grok-4-1-fast-non-reasoning")
AUTO_REFRESH_MS = int(os.getenv("AUTO_REFRESH_MS", "30000"))
MAX_CHAT_MESSAGES = max(50, min(int(os.getenv("MAX_CHAT_MESSAGES", "300")), 5000))
MAX_AGENT_MESSAGES = max(50, min(int(os.getenv("MAX_AGENT_MESSAGES", "180")), 8000))
WINDOWS_OS_ALERTS = os.getenv("WINDOWS_OS_ALERTS", "true").strip().lower() in {"1", "true", "yes", "on"}
WINDOWS_OS_ALERT_TIMEOUT_SECONDS = max(1, min(int(os.getenv("WINDOWS_OS_ALERT_TIMEOUT_SECONDS", "1800")), 86400))

st.set_page_config(page_title="Schedule Chatbot", page_icon="chat", layout="wide")


@st.cache_resource
def get_runtime():
    return create_runtime(model_name=MODEL_NAME)


runtime = get_runtime()

st.markdown(
    """
<style>
.block-container {
    padding-top: 1.35rem;
}
[data-testid="stSidebarContent"] {
    background:
        radial-gradient(120% 65% at 0% 0%, #e8f2ff 0%, rgba(232, 242, 255, 0) 60%),
        linear-gradient(180deg, #f8fbff 0%, #eef3f9 100%);
    border-right: 1px solid #dbe3ef;
}
div[class*="st-key-taskcard_"] {
    position: relative;
    border: 1px solid #d4ddec;
    border-radius: 16px;
    padding: 0.78rem 0.78rem 0.7rem;
    margin-bottom: 0.72rem;
    background: linear-gradient(145deg, #ffffff 0%, #f7fbff 100%);
    box-shadow: 0 12px 24px -22px #1f2937;
    transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
}
div[class*="st-key-taskcard_"]::before {
    content: "";
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 4px;
    border-radius: 16px 0 0 16px;
    background: linear-gradient(180deg, #2563eb 0%, #0ea5e9 45%, #10b981 100%);
}
div[class*="st-key-taskcard_"]:hover {
    transform: translateY(-1px);
    border-color: #b8c7de;
    box-shadow: 0 16px 28px -22px #1f2937;
}
.task-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.45rem;
}
.task-id {
    font-size: 0.75rem;
    color: #334155;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: #eef4ff;
    border: 1px solid #d9e6ff;
    border-radius: 999px;
    padding: 0.14rem 0.44rem;
    letter-spacing: 0.01em;
}
.task-badge {
    font-size: 0.7rem;
    font-weight: 700;
    padding: 0.16rem 0.5rem;
    border-radius: 999px;
    border: 1px solid transparent;
}
.task-badge-active {
    background: #e7f8ef;
    border-color: #bde5cb;
    color: #116432;
}
.task-badge-paused {
    background: #fff4e5;
    border-color: #ffd9a8;
    color: #8a4b00;
}
.task-row {
    font-size: 0.82rem;
    color: #273449;
    margin: 0.2rem 0;
}
.task-row-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.55rem;
    flex-wrap: wrap;
}
.task-row strong {
    color: #132238;
    font-weight: 700;
}
.task-prompt {
    margin-top: 0.56rem;
    padding-top: 0.56rem;
    padding-bottom: 0.46rem;
    border-top: 1px dashed #d7e0ee;
    font-size: 0.88rem;
    color: #111f33;
    line-height: 1.42;
}
.task-topline {
    display: flex;
    align-items: center;
    gap: 0.45rem;
}
div[class*="st-key-delete_task_"] button {
    border-radius: 10px;
    border: 1px solid #f2c3c3;
    background: #fff5f5;
    color: #b42318;
    min-height: 2rem;
    font-weight: 700;
    white-space: nowrap;
}
div[class*="st-key-delete_task_"] button:hover {
    border-color: #e59a9a;
    background: #ffe8e8;
    color: #931f16;
}
div[class*="st-key-pause_task_"] button {
    border-radius: 10px;
    border: 1px solid #ffd8b3;
    background: #fff6eb;
    color: #a04a00;
    min-height: 2rem;
    font-weight: 700;
    white-space: nowrap;
}
div[class*="st-key-pause_task_"] button:hover {
    border-color: #ffc68f;
    background: #ffedd9;
}
div[class*="st-key-resume_task_"] button {
    border-radius: 10px;
    border: 1px solid #b9e5c8;
    background: #ecfbf2;
    color: #15623a;
    min-height: 2rem;
    font-weight: 700;
    white-space: nowrap;
}
div[class*="st-key-resume_task_"] button:hover {
    border-color: #9fdab5;
    background: #ddf6e8;
}
div[class*="st-key-run_task_"] button {
    border-radius: 10px;
    border: 1px solid #b7cff8;
    background: #edf4ff;
    color: #1e4ca3;
    min-height: 2rem;
    font-weight: 700;
    white-space: nowrap;
}
div[class*="st-key-run_task_"] button:hover {
    border-color: #9dbcf3;
    background: #e2eeff;
}
div[class*="st-key-save_task_"] button {
    border-radius: 10px;
    border: 1px solid #b7cff8;
    background: #edf4ff;
    color: #1e4ca3;
    min-height: 2rem;
    font-weight: 700;
}
div[class*="st-key-save_task_"] button:hover {
    border-color: #9dbcf3;
    background: #e2eeff;
}
div[class*="st-key-edit_task_"] button {
    border-radius: 10px;
    border: 1px solid #c6d8f7;
    background: #f0f6ff;
    color: #2451a6;
    min-height: 2rem;
    font-weight: 700;
}
div[class*="st-key-edit_task_"] button:hover {
    border-color: #a9c5f0;
    background: #e6f0ff;
}
div[class*="st-key-cancel_task_edit_"] button {
    border-radius: 10px;
    border: 1px solid #d7dfeb;
    background: #f7f9fc;
    color: #334155;
    min-height: 2rem;
    font-weight: 700;
}
div[class*="st-key-cancel_task_edit_"] button:hover {
    border-color: #c4d0e2;
    background: #f0f4f9;
}
div[class*="st-key-task_prompt_draft_"] textarea {
    border-radius: 12px;
    border: 1px solid #d3dded;
    background: #fbfdff;
    color: #10233b;
    line-height: 1.35;
}
@media (max-width: 768px) {
    div[class*="st-key-taskcard_"] {
        padding: 0.72rem 0.7rem 0.64rem;
        margin-bottom: 0.62rem;
    }
    div[class*="st-key-taskcard_"]:hover {
        transform: none;
    }
    .task-row {
        font-size: 0.8rem;
    }
    .task-prompt {
        font-size: 0.85rem;
    }
}
</style>
""",
    unsafe_allow_html=True,
)


def format_dt_for_tz(utc_iso: str | None, tz_name: str) -> str:
    return format_cz_datetime(utc_iso, tz_name)


def get_user_timezone_name() -> str:
    tz_name = (os.getenv("USER_TIMEZONE", "") or "").strip()
    if tz_name:
        return tz_name
    try:
        return str(datetime.now().astimezone().tzinfo or "UTC")
    except Exception:
        return "UTC"


def format_event_timestamp(utc_iso: str | None) -> str:
    return format_cz_datetime(utc_iso, get_user_timezone_name())


def render_tool_calls_panel(tool_calls: list | None) -> None:
    if not isinstance(tool_calls, list) or not tool_calls:
        st.text("Tool called: No")
        return

    lines = []
    for idx, call in enumerate(tool_calls, start=1):
        if not isinstance(call, dict):
            continue
        name = str(call.get("name", "unknown"))
        args = call.get("args", {})
        try:
            args_text = json.dumps(args, ensure_ascii=False, separators=(",", ": "))
        except Exception:
            args_text = str(args)
        lines.append(f"{idx}. {name} args={args_text}")

    if not lines:
        st.text("Tool called: No")
        return

    st.text("Tool called: Yes")
    with st.expander("Tool calls", expanded=False):
        st.code("\n".join(lines), language="text")


def should_render_tool_calls(message: dict) -> bool:
    if message.get("role") != "assistant":
        return False

    message_type = str(message.get("message_type", "") or "").strip().lower()
    if message_type:
        return message_type in {"ai", "scheduled"}

    # Backward compatibility for persisted history written before message_type existed.
    return "tool_calls" in message

def render_task_card(task: dict) -> dict | None:
    action: dict | None = None
    enabled = bool(task.get("enabled", True))
    status = "Active" if task.get("enabled", True) else "Paused"
    status_class = "task-badge-active" if status == "Active" else "task-badge-paused"
    tz_name = task.get("timezone", "UTC")
    next_run_local = format_dt_for_tz(task.get("next_run_utc"), tz_name)
    schedule = escape(task.get("schedule_text", ""))
    raw_prompt = str(task.get("task_prompt", ""))
    prompt = escape(raw_prompt)
    task_id_raw = str(task.get("id", ""))
    task_id = escape(task_id_raw)
    tz_name_safe = escape(tz_name)
    editing_key = f"editing_task_{task_id_raw}"
    draft_key = f"task_prompt_draft_{task_id_raw}"
    is_editing = bool(st.session_state.get(editing_key, False))

    if draft_key not in st.session_state:
        st.session_state[draft_key] = raw_prompt
    elif not is_editing:
        # Keep inline editor draft in sync with persisted text when not actively editing.
        st.session_state[draft_key] = raw_prompt

    with st.container(border=False, key=f"taskcard_{task_id_raw}"):
        toggle_col, run_col, delete_col = st.columns([0.3, 0.35, 0.35], vertical_alignment="center")
        with toggle_col:
            if enabled:
                pause_clicked = bool(
                    task_id_raw
                    and st.button(
                        "Pause",
                        key=f"pause_task_{task_id_raw}",
                        help=f"Pause task {task_id_raw}",
                        use_container_width=True,
                    )
                )
                if pause_clicked:
                    action = {"type": "set_enabled", "task_id": task_id_raw, "enabled": False}
            else:
                resume_clicked = bool(
                    task_id_raw
                    and st.button(
                        "Resume",
                        key=f"resume_task_{task_id_raw}",
                        help=f"Resume task {task_id_raw}",
                        use_container_width=True,
                    )
                )
                if resume_clicked:
                    action = {"type": "set_enabled", "task_id": task_id_raw, "enabled": True}
        with run_col:
            run_clicked = bool(
                task_id_raw
                and st.button(
                    "Run now",
                    key=f"run_task_{task_id_raw}",
                    help=f"Run task {task_id_raw} immediately",
                    use_container_width=True,
                )
            )
            if run_clicked:
                action = {"type": "run_now", "task_id": task_id_raw}
        with delete_col:
            delete_clicked = bool(
                task_id_raw
                and st.button(
                    "Delete",
                    key=f"delete_task_{task_id_raw}",
                    help=f"Delete task {task_id_raw}",
                    use_container_width=True,
                )
            )
            if delete_clicked:
                action = {"type": "delete", "task_id": task_id_raw}
        st.markdown(f'<div class="task-row"><strong>Schedule:</strong> {schedule}</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="task-row task-row-meta">'
            f'<span><strong>Next run:</strong> {next_run_local} ({tz_name_safe})</span>'
            f'<span class="task-topline"><span class="task-id">{task_id}</span>'
            f'<span class="task-badge {status_class}">{status}</span></span>'
            "</div>",
            unsafe_allow_html=True,
        )
        if is_editing:
            edited_prompt = st.text_area(
                "Task text",
                key=draft_key,
                label_visibility="collapsed",
                height=95,
            )
            save_col, cancel_col = st.columns([0.5, 0.5], vertical_alignment="center")
            with save_col:
                save_clicked = bool(
                    task_id_raw
                    and st.button(
                        "Save text",
                        key=f"save_task_{task_id_raw}",
                        help=f"Save new task text for {task_id_raw}",
                        use_container_width=True,
                    )
                )
            with cancel_col:
                cancel_clicked = bool(
                    task_id_raw
                    and st.button(
                        "Cancel",
                        key=f"cancel_task_edit_{task_id_raw}",
                        help=f"Cancel editing task {task_id_raw}",
                        use_container_width=True,
                    )
                )
            if save_clicked:
                action = {
                    "type": "update_prompt",
                    "task_id": task_id_raw,
                    "task_prompt": edited_prompt,
                }
            elif cancel_clicked:
                action = {
                    "type": "cancel_edit",
                    "task_id": task_id_raw,
                    "task_prompt": raw_prompt,
                }
        else:
            st.markdown(f'<div class="task-prompt">{prompt}</div>', unsafe_allow_html=True)
            edit_clicked = bool(
                task_id_raw
                and st.button(
                    "Edit text",
                    key=f"edit_task_{task_id_raw}",
                    help=f"Edit task text for {task_id_raw}",
                    use_container_width=True,
                )
            )
            if edit_clicked:
                action = {
                    "type": "start_edit",
                    "task_id": task_id_raw,
                    "task_prompt": raw_prompt,
                }
    return action


def to_agent_messages(chat_messages: list[dict]) -> list:
    converted = []
    for message in chat_messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "user":
            converted.append(HumanMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
    return converted


def initial_assistant_message() -> dict:
    return {
        "role": "assistant",
        "content": "How can I help? I can answer directly or schedule recurring tasks.",
        "message_type": "system",
    }


def trim_session_histories() -> None:
    st.session_state.messages = st.session_state.messages[-MAX_CHAT_MESSAGES:]
    st.session_state.agent_messages = st.session_state.agent_messages[-MAX_AGENT_MESSAGES:]


def send_windows_os_alert(message: str) -> None:
    if os.name != "nt" or not WINDOWS_OS_ALERTS:
        return
    try:
        compact = " ".join((message or "").split())
        if not compact:
            return
        compact = compact[:220]
        subprocess.run(
            ["msg", "*", f"/TIME:{WINDOWS_OS_ALERT_TIMEOUT_SECONDS}", compact],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        pass


if "messages" not in st.session_state:
    persisted_messages = runtime.store.get_chat_messages()
    if persisted_messages:
        st.session_state.messages = persisted_messages
    else:
        initial_message = initial_assistant_message()
        st.session_state.messages = [initial_message]
        runtime.store.append_chat_message(
            role=initial_message["role"],
            content=initial_message["content"],
            message_type=initial_message.get("message_type"),
        )

if "agent_messages" not in st.session_state:
    st.session_state.agent_messages = to_agent_messages(st.session_state.messages)

trim_session_histories()

if "last_event_id" not in st.session_state:
    st.session_state.last_event_id = runtime.store.get_processed_event_id()


st.title("Schedule Assistant")

with st.sidebar:
    clear_clicked = st.button("Clear Conversation", use_container_width=True)
    if clear_clicked:
        runtime.store.clear_chat_messages()
        fresh = initial_assistant_message()
        st.session_state.messages = [fresh]
        st.session_state.agent_messages = [AIMessage(content=fresh["content"])]
        runtime.store.append_chat_message(
            role=fresh["role"],
            content=fresh["content"],
            message_type=fresh.get("message_type"),
        )
        st.rerun()

    st.subheader("Scheduled Tasks")

    tasks = runtime.store.list_tasks()
    if tasks:
        def _sort_key(t: dict) -> str:
            return t.get("next_run_utc") or "9999-12-31T23:59:59+00:00"

        for task in sorted(tasks, key=_sort_key):
            action = render_task_card(task)
            if action:
                action_type = str(action.get("type", ""))
                task_id = str(action.get("task_id", ""))
                editing_key = f"editing_task_{task_id}"
                draft_key = f"task_prompt_draft_{task_id}"
                notice: str | None = None

                if action_type == "delete":
                    removed = runtime.store.remove_task(task_id)
                    notice = (
                        f"Task {task_id} was deleted manually."
                        if removed
                        else f"Task {task_id} was not found."
                    )
                    if removed:
                        st.session_state.pop(editing_key, None)
                elif action_type == "run_now":
                    notice = trigger_task_now(
                        scheduler_agent=runtime.scheduler_agent,
                        store=runtime.store,
                        scheduler_loop=runtime.scheduler_loop,
                        task_id=task_id,
                    )
                elif action_type == "set_enabled":
                    desired_enabled = bool(action.get("enabled", True))
                    updated = runtime.store.set_task_enabled(task_id, desired_enabled)
                    if updated:
                        if desired_enabled:
                            resumed_tz = str(updated.get("timezone") or get_user_timezone_name())
                            next_run_local = format_dt_for_tz(updated.get("next_run_utc"), resumed_tz)
                            notice = f"Task {task_id} resumed. Next run: {next_run_local} ({resumed_tz})."
                        else:
                            notice = f"Task {task_id} paused."
                    else:
                        notice = f"Task {task_id} was not found."
                elif action_type == "start_edit":
                    st.session_state[editing_key] = True
                    st.session_state[draft_key] = str(action.get("task_prompt", ""))
                    st.rerun()
                elif action_type == "cancel_edit":
                    st.session_state[editing_key] = False
                    st.rerun()
                elif action_type == "update_prompt":
                    new_prompt = str(action.get("task_prompt", "")).strip()
                    if not new_prompt:
                        notice = f"Task {task_id} text cannot be empty."
                    else:
                        updated = runtime.store.update_task_prompt(task_id, new_prompt)
                        notice = (
                            f"Task {task_id} text updated."
                            if updated
                            else f"Task {task_id} was not found."
                        )
                        if updated:
                            st.session_state[editing_key] = False

                if notice is not None:
                    manual_message = {"role": "assistant", "content": notice, "message_type": "system"}
                    st.session_state.messages.append(manual_message)
                    st.session_state.agent_messages.append(AIMessage(content=notice))
                    runtime.store.append_chat_message(
                        role=manual_message["role"],
                        content=manual_message["content"],
                        message_type=manual_message["message_type"],
                    )
                    trim_session_histories()
                    st.rerun()
    else:
        st.info("No scheduled tasks.")


new_events = runtime.store.get_events_after(st.session_state.last_event_id)
for event in new_events:
    if event.get("type") == "scheduled_task_result":
        created_at_text = format_event_timestamp(event.get("created_at"))
        result_text = str(event.get("message", "") or "")
        scheduled_text = (
            f"**Scheduled task completed at {created_at_text}**\n\n"
            f"{result_text}"
        )
        scheduled_message = {
            "role": "assistant",
            "content": scheduled_text,
            "tool_calls": event.get("tool_calls", []),
            "message_type": "scheduled",
        }
        st.session_state.messages.append(scheduled_message)
        st.session_state.agent_messages.append(AIMessage(content=scheduled_text))
        runtime.store.append_chat_message(
            role=scheduled_message["role"],
            content=scheduled_message["content"],
            tool_calls=scheduled_message["tool_calls"],
            message_type=scheduled_message["message_type"],
        )
        send_windows_os_alert(f"Scheduled task {event.get('task_id')} completed. Open Schedule Assistant.")
        trim_session_histories()
if new_events:
    latest_event_id = new_events[-1]["id"]
    st.session_state.last_event_id = latest_event_id
    runtime.store.set_processed_event_id(latest_event_id)


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
    if should_render_tool_calls(message):
        render_tool_calls_panel(message.get("tool_calls"))


prompt = st.chat_input("Message...")
if prompt:
    task_ids_before = {task.get("id") for task in runtime.store.list_tasks()}

    user_message = {"role": "user", "content": prompt}
    st.session_state.messages.append(user_message)
    st.session_state.agent_messages.append(HumanMessage(content=prompt))
    runtime.store.append_chat_message(role=user_message["role"], content=user_message["content"])
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Working..."):
            try:
                detailed = invoke_agent_detailed(runtime.agent, st.session_state.agent_messages)
                response = detailed["text"]
                response_tool_calls = detailed.get("tool_calls", [])
                # Keep full model+tool history for next turn context.
                st.session_state.agent_messages = detailed["messages"]
            except Exception as exc:
                response = f"Agent error: {exc}"
                response_tool_calls = []
            st.markdown(response)
    render_tool_calls_panel(response_tool_calls)

    assistant_message = {"role": "assistant", "content": response, "tool_calls": response_tool_calls, "message_type": "ai"}
    st.session_state.messages.append(assistant_message)
    runtime.store.append_chat_message(
        role=assistant_message["role"],
        content=assistant_message["content"],
        tool_calls=assistant_message["tool_calls"],
        message_type=assistant_message["message_type"],
    )
    trim_session_histories()

    task_ids_after = {task.get("id") for task in runtime.store.list_tasks()}
    if task_ids_before != task_ids_after:
        st.rerun()

# Keep UI polling active so scheduled-task events appear without manual browser refresh.
st_autorefresh(interval=AUTO_REFRESH_MS, key="chat_auto_refresh")


