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

from runtime import create_runtime, invoke_agent_streaming_detailed, trigger_task_now
from runtime_store import format_cz_datetime


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
    _ = runtime_schema_version  # cache-busting version for runtime/tool schema migrations
    return create_runtime(model_name=MODEL_NAME)


runtime = get_runtime(RUNTIME_SCHEMA_VERSION)

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
div[class*="st-key-taskcard_active_"] {
    border-color: #c7d9f4;
    background: linear-gradient(145deg, #f1f7ff 0%, #e7f1ff 100%);
}
div[class*="st-key-taskcard_paused_"] {
    border-color: #ead7ba;
    background: linear-gradient(145deg, #fff8ee 0%, #fff1df 100%);
}
div[class*="st-key-taskgroup_"] [data-testid="stExpander"] details {
    border: 1px solid #d7e1ef;
    border-radius: 18px;
    background: linear-gradient(180deg, #f9fbff 0%, #f1f6fd 100%);
    box-shadow: 0 12px 24px -26px #1f2937;
    overflow: hidden;
    margin-bottom: 0.85rem;
}
div[class*="st-key-taskgroup_"] [data-testid="stExpander"] details summary {
    padding-top: 0.16rem;
    padding-bottom: 0.16rem;
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
div[class*="st-key-taskcard_"] [data-testid="stExpander"] details {
    border: 1px solid rgba(191, 208, 232, 0.95);
    border-radius: 14px;
    background: rgba(255, 255, 255, 0.5);
    margin-top: 0.62rem;
    overflow: hidden;
}
div[class*="st-key-taskcard_"] [data-testid="stExpander"] details summary {
    padding-top: 0.08rem;
    padding-bottom: 0.08rem;
}
div[class*="st-key-taskcard_"] [data-testid="stExpander"] details[open] {
    background: rgba(255, 255, 255, 0.74);
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
.task-title {
    margin: 0.08rem 0 0.34rem;
    font-size: 0.94rem;
    color: #0f2138;
    line-height: 1.35;
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
    padding: 0.1rem 0 0.46rem;
    font-size: 0.88rem;
    color: #111f33;
    line-height: 1.42;
}
.task-details-label {
    font-size: 0.76rem;
    color: #3d5a83;
    font-weight: 700;
    letter-spacing: 0.01em;
    text-transform: uppercase;
}
.empty-chat-subtitle {
    margin-top: -0.35rem;
    margin-bottom: 0.85rem;
    font-size: 1rem;
    color: #4b6382;
    font-weight: 600;
    letter-spacing: 0.01em;
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
div[class*="st-key-scheduled_result_"] [data-testid="stExpander"] details {
    border: 1px solid #d4ddec;
    border-radius: 16px;
    background: linear-gradient(145deg, #ffffff 0%, #f8fbff 100%);
    box-shadow: 0 12px 24px -22px #1f2937;
    overflow: hidden;
}
div[class*="st-key-scheduled_result_"] [data-testid="stExpander"] details summary {
    padding-top: 0.12rem;
    padding-bottom: 0.12rem;
}
div[class*="st-key-scheduled_result_read_"] [data-testid="stExpander"] details:not([open]) {
    border-color: #d5dfec;
    background: #f1f6fb;
}
div[class*="st-key-scheduled_result_unread_"] [data-testid="stExpander"] details:not([open]) {
    border-color: #8ab4ff;
    background:
        radial-gradient(140% 120% at 0% 0%, rgba(96, 165, 250, 0.22) 0%, rgba(96, 165, 250, 0) 58%),
        linear-gradient(145deg, #eff6ff 0%, #dbeafe 100%);
    box-shadow: 0 16px 30px -24px #2563eb;
}
div[class*="st-key-scheduled_result_"] [data-testid="stExpander"] details[open] {
    border-color: #bfd0e8;
    background: linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
}
div[class*="st-key-mark_scheduled_read_"] button {
    border-radius: 10px;
    border: 1px solid #b7cff8;
    background: #edf4ff;
    color: #1e4ca3;
    min-height: 2rem;
    font-weight: 700;
}
div[class*="st-key-mark_scheduled_read_"] button:hover {
    border-color: #9dbcf3;
    background: #e2eeff;
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


def local_dt_for_tz(utc_iso: str | None, tz_name: str) -> datetime | None:
    if not utc_iso:
        return None
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")

    try:
        raw = str(utc_iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)
    except Exception:
        return None


def classify_task_bucket(task: dict) -> str:
    tz_name = str(task.get("timezone", "UTC") or "UTC")
    next_run_local = local_dt_for_tz(task.get("next_run_utc"), tz_name)
    if next_run_local is None:
        return "future"

    today = datetime.now(next_run_local.tzinfo).date()
    if next_run_local.date() <= today:
        return "today"
    if next_run_local.date().toordinal() == today.toordinal() + 1:
        return "tomorrow"
    return "future"


def format_task_next_run_html(task: dict) -> str:
    tz_name = str(task.get("timezone", "UTC") or "UTC")
    tz_name_safe = escape(tz_name)
    next_run_local = local_dt_for_tz(task.get("next_run_utc"), tz_name)
    if next_run_local is None:
        return f"<strong>-</strong> ({tz_name_safe})"

    today = datetime.now(next_run_local.tzinfo).date()
    if next_run_local.date() == today:
        label = f"Today - {next_run_local.strftime('%H:%M')}"
    elif next_run_local.date().toordinal() == today.toordinal() + 1:
        label = f"Tomorrow - {next_run_local.strftime('%H:%M')}"
    else:
        label = f"{next_run_local.day}. {next_run_local.month}. {next_run_local.year} - {next_run_local.strftime('%H:%M')}"
    return f"<strong>{escape(label)}</strong> ({tz_name_safe})"


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
        st.info("No tools were used for this reply.")
        return

    entries: list[tuple[int, str, object]] = []
    for idx, call in enumerate(tool_calls, start=1):
        if not isinstance(call, dict):
            continue
        name = str(call.get("name", "unknown"))
        args = call.get("args", {})
        entries.append((idx, name, args))

    if not entries:
        st.info("No tools were used for this reply.")
        return

    st.info(f"Used {len(entries)} tool call(s).")
    with st.expander("Tool calls", expanded=False):
        for idx, name, args in entries:
            with st.container(border=True):
                st.markdown(f"**{idx}. `{name}`**")
                if isinstance(args, (dict, list)):
                    st.code(json.dumps(args, ensure_ascii=False, indent=2), language="json")
                else:
                    try:
                        args_text = json.dumps(args, ensure_ascii=False, indent=2)
                    except Exception:
                        args_text = str(args)
                    st.code(args_text, language="text")


def should_render_tool_calls(message: dict) -> bool:
    if message.get("role") != "assistant":
        return False

    message_type = str(message.get("message_type", "") or "").strip().lower()
    if message_type:
        return message_type == "ai"

    # Backward compatibility for persisted history written before message_type existed.
    return "tool_calls" in message


def is_scheduled_result_message(message: dict) -> bool:
    return str(message.get("message_type", "") or "").strip().lower() == "scheduled"


def scheduled_result_status_text(message: dict) -> str:
    status = str(message.get("scheduled_status", "") or "").strip().lower()
    created_at_text = format_event_timestamp(message.get("scheduled_created_at"))
    if status == "retry_scheduled":
        return f"Retry planned • {created_at_text}"
    if status == "failed_no_retry":
        return f"Failed, no retry • {created_at_text}"
    return f"Completed • {created_at_text}"


def render_scheduled_result_message(message: dict, index: int) -> None:
    scheduled_event_id = str(message.get("scheduled_event_id", "") or "").strip()
    if not scheduled_event_id:
        st.markdown(str(message.get("content", "") or ""))
        render_tool_calls_panel(message.get("tool_calls"))
        return

    unread_ids = st.session_state.setdefault("unread_scheduled_result_ids", set())
    is_unread = scheduled_event_id in unread_ids
    raw_title = " ".join(str(message.get("task_title", "")).split()).strip()
    task_title = raw_title or str(message.get("task_id", "")).strip() or f"Scheduled task #{index + 1}"
    status_text = scheduled_result_status_text(message)
    label_parts = [task_title]
    if is_unread:
        label_parts.append("Unread")
    label_parts.append(status_text)
    expander_label = " | ".join(part for part in label_parts if part)
    container_state = "unread" if is_unread else "read"

    with st.container(border=False, key=f"scheduled_result_{container_state}_{scheduled_event_id}"):
        with st.expander(expander_label, expanded=False):
            meta_bits = []
            task_id = str(message.get("task_id", "") or "").strip()
            if task_id:
                meta_bits.append(f"Task ID: `{task_id}`")
            meta_bits.append(status_text)
            st.caption(" • ".join(meta_bits))
            if is_unread:
                if st.button("Mark as read", key=f"mark_scheduled_read_{scheduled_event_id}"):
                    unread_ids.discard(scheduled_event_id)
                    st.rerun()
            st.markdown(str(message.get("content", "") or ""))
            render_tool_calls_panel(message.get("tool_calls"))

def render_task_card(task: dict) -> dict | None:
    action: dict | None = None
    enabled = bool(task.get("enabled", True))
    status = "Active" if task.get("enabled", True) else "Paused"
    status_class = "task-badge-active" if status == "Active" else "task-badge-paused"
    taskcard_state = "active" if enabled else "paused"
    tz_name = task.get("timezone", "UTC")
    next_run_html = format_task_next_run_html(task)
    schedule = escape(task.get("schedule_text", ""))
    raw_prompt = str(task.get("task_prompt", ""))
    prompt = escape(raw_prompt)
    task_id_raw = str(task.get("id", ""))
    raw_title = " ".join(str(task.get("title", "")).split()).strip() or task_id_raw or "Untitled task"
    task_title = escape(raw_title)
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

    with st.container(border=False, key=f"taskcard_{taskcard_state}_{task_id_raw}"):
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
        st.markdown(f'<div class="task-title"><strong>{task_title}</strong></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="task-row"><strong>Schedule:</strong> {schedule}</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="task-row task-row-meta">'
            f'<span><strong>Next run:</strong> {next_run_html}</span>'
            f'<span class="task-topline"><span class="task-id">{task_id}</span>'
            f'<span class="task-badge {status_class}">{status}</span></span>'
            "</div>",
            unsafe_allow_html=True,
        )
        with st.expander("Task details", expanded=is_editing):
            st.markdown('<div class="task-details-label">Task text</div>', unsafe_allow_html=True)
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


def handle_task_card_action(action: dict) -> None:
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
            executor_agent=runtime.executor_agent,
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
                next_run_html = format_task_next_run_html(updated)
                next_run_text = next_run_html.replace("<strong>", "").replace("</strong>", "")
                notice = f"Task {task_id} resumed. Next run: {next_run_text}."
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


def is_legacy_intro_message(message: dict) -> bool:
    if not isinstance(message, dict):
        return False
    return (
        str(message.get("role", "")).strip().lower() == "assistant"
        and str(message.get("message_type", "")).strip().lower() == "system"
        and str(message.get("content", "")).strip() == LEGACY_INTRO_MESSAGE
    )


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


def render_page_header(target: "st.delta_generator.DeltaGenerator") -> None:
    with target.container():
        st.title("Schedule Assistant")
        if not st.session_state.messages:
            st.markdown(f'<div class="empty-chat-subtitle">{escape(EMPTY_CHAT_SUBTITLE)}</div>', unsafe_allow_html=True)


if "messages" not in st.session_state:
    persisted_messages = [message for message in runtime.store.get_chat_messages() if not is_legacy_intro_message(message)]
    if persisted_messages:
        st.session_state.messages = persisted_messages
    else:
        st.session_state.messages = []

if "agent_messages" not in st.session_state:
    st.session_state.agent_messages = to_agent_messages(st.session_state.messages)

trim_session_histories()

if "last_event_id" not in st.session_state:
    st.session_state.last_event_id = runtime.store.get_processed_event_id()

if "unread_scheduled_result_ids" not in st.session_state:
    st.session_state.unread_scheduled_result_ids = set()


header_placeholder = st.empty()
render_page_header(header_placeholder)

with st.sidebar:
    clear_clicked = st.button("Clear Conversation", use_container_width=True)
    if clear_clicked:
        runtime.store.clear_chat_messages()
        st.session_state.messages = []
        st.session_state.agent_messages = []
        st.rerun()

    st.subheader("Scheduled Tasks")

    tasks = runtime.store.list_tasks()
    if tasks:
        def _sort_key(t: dict) -> str:
            return t.get("next_run_utc") or "9999-12-31T23:59:59+00:00"

        grouped_tasks = {"today": [], "tomorrow": [], "future": []}
        for task in sorted(tasks, key=_sort_key):
            grouped_tasks[classify_task_bucket(task)].append(task)

        group_specs = [
            ("today", "Today's Tasks", True),
            ("tomorrow", "Tomorrow's Tasks", False),
            ("future", "Future Tasks", False),
        ]

        for group_key, group_label, expanded in group_specs:
            group_tasks = grouped_tasks[group_key]
            if not group_tasks:
                continue

            with st.container(border=False, key=f"taskgroup_{group_key}"):
                with st.expander(f"{group_label} ({len(group_tasks)})", expanded=expanded):
                    for task in group_tasks:
                        action = render_task_card(task)
                        if action:
                            handle_task_card_action(action)
    else:
        st.info("No scheduled tasks.")


new_events = runtime.store.get_events_after(st.session_state.last_event_id)
for event in new_events:
    if event.get("type") == "scheduled_task_result":
        result_text = str(event.get("message", "") or "")
        task_title = " ".join(str(event.get("task_title", "")).split()).strip()
        scheduled_text = result_text
        scheduled_event_id = str(event.get("id", ""))
        scheduled_message = {
            "role": "assistant",
            "content": scheduled_text,
            "tool_calls": event.get("tool_calls", []),
            "message_type": "scheduled",
            "scheduled_event_id": scheduled_event_id,
            "scheduled_created_at": event.get("created_at"),
            "scheduled_status": event.get("status", "completed"),
            "task_id": event.get("task_id"),
            "task_title": task_title or event.get("task_id"),
        }
        st.session_state.messages.append(scheduled_message)
        st.session_state.agent_messages.append(AIMessage(content=scheduled_text))
        runtime.store.append_chat_message(
            role=scheduled_message["role"],
            content=scheduled_message["content"],
            tool_calls=scheduled_message["tool_calls"],
            message_type=scheduled_message["message_type"],
            extra_fields={
                "scheduled_event_id": scheduled_message["scheduled_event_id"],
                "scheduled_created_at": scheduled_message["scheduled_created_at"],
                "scheduled_status": scheduled_message["scheduled_status"],
                "task_id": scheduled_message["task_id"],
                "task_title": scheduled_message["task_title"],
            },
        )
        st.session_state.unread_scheduled_result_ids.add(scheduled_event_id)
        send_windows_os_alert(f"Scheduled task {event.get('task_id')} completed. Open Schedule Assistant.")
        trim_session_histories()
if new_events:
    latest_event_id = new_events[-1]["id"]
    st.session_state.last_event_id = latest_event_id
    runtime.store.set_processed_event_id(latest_event_id)


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
    task_ids_before = {task.get("id") for task in runtime.store.list_tasks()}

    user_message = {"role": "user", "content": prompt}
    st.session_state.messages.append(user_message)
    st.session_state.agent_messages.append(HumanMessage(content=prompt))
    runtime.store.append_chat_message(role=user_message["role"], content=user_message["content"])
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
            # Keep full model+tool history for next turn context.
            st.session_state.agent_messages = detailed["messages"]
        except Exception as exc:
            response = f"Agent error: {exc}"
            response_tool_calls = []
        response_placeholder.markdown(response)
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
    st.rerun()

render_page_header(header_placeholder)

# Keep UI polling active so scheduled-task events appear without manual browser refresh.
st_autorefresh(interval=AUTO_REFRESH_MS, key="chat_auto_refresh")


