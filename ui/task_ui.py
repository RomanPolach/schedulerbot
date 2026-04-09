from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from langchain_core.messages import AIMessage

from runtime import trigger_task_now
from runtime_store import format_cz_datetime


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
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)
    except Exception:
        return None


def classify_task_bucket(task: dict[str, Any]) -> str:
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


def format_task_next_run_html(task: dict[str, Any]) -> str:
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


def render_task_card(task: dict[str, Any]) -> dict[str, Any] | None:
    action: dict[str, Any] | None = None
    enabled = bool(task.get("enabled", True))
    status = "Active" if enabled else "Paused"
    status_class = "task-badge-active" if enabled else "task-badge-paused"
    taskcard_state = "active" if enabled else "paused"
    schedule = escape(str(task.get("schedule_text", "")))
    raw_prompt = str(task.get("task_prompt", ""))
    prompt = escape(raw_prompt)
    task_id_raw = str(task.get("id", ""))
    raw_title = " ".join(str(task.get("title", "")).split()).strip() or task_id_raw or "Untitled task"
    task_title = escape(raw_title)
    next_run_html = format_task_next_run_html(task)
    editing_key = f"editing_task_{task_id_raw}"
    draft_key = f"task_prompt_draft_{task_id_raw}"
    is_editing = bool(st.session_state.get(editing_key, False))

    if draft_key not in st.session_state:
        st.session_state[draft_key] = raw_prompt
    elif not is_editing:
        st.session_state[draft_key] = raw_prompt

    with st.container(border=False, key=f"taskcard_{taskcard_state}_{task_id_raw}"):
        toggle_col, run_col, delete_col = st.columns([0.3, 0.35, 0.35], vertical_alignment="center")
        with toggle_col:
            label = "Pause" if enabled else "Resume"
            key_prefix = "pause" if enabled else "resume"
            desired_enabled = not enabled
            if task_id_raw and st.button(
                label,
                key=f"{key_prefix}_task_{task_id_raw}",
                help=f"{label} task {task_id_raw}",
                use_container_width=True,
            ):
                action = {"type": "set_enabled", "task_id": task_id_raw, "enabled": desired_enabled}
        with run_col:
            if task_id_raw and st.button(
                "Run now",
                key=f"run_task_{task_id_raw}",
                help=f"Run task {task_id_raw} immediately",
                use_container_width=True,
            ):
                action = {"type": "run_now", "task_id": task_id_raw}
        with delete_col:
            if task_id_raw and st.button(
                "Delete",
                key=f"delete_task_{task_id_raw}",
                help=f"Delete task {task_id_raw}",
                use_container_width=True,
            ):
                action = {"type": "delete", "task_id": task_id_raw}

        st.markdown(
            '<div class="task-title-row">'
            f'<div class="task-title"><strong>{task_title}</strong></div>'
            f'<span class="task-badge {status_class}">{status}</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        st.markdown(f'<div class="task-row"><strong>Schedule:</strong> {schedule}</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="task-row task-row-meta">'
            f'<span><strong>Next run:</strong> {next_run_html}</span>'
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
                    action = {"type": "update_prompt", "task_id": task_id_raw, "task_prompt": edited_prompt}
                elif cancel_clicked:
                    action = {"type": "cancel_edit", "task_id": task_id_raw, "task_prompt": raw_prompt}
            else:
                st.markdown(f'<div class="task-prompt">{prompt}</div>', unsafe_allow_html=True)
                if task_id_raw and st.button(
                    "Edit text",
                    key=f"edit_task_{task_id_raw}",
                    help=f"Edit task text for {task_id_raw}",
                    use_container_width=True,
                ):
                    action = {"type": "start_edit", "task_id": task_id_raw, "task_prompt": raw_prompt}
    return action


def handle_task_card_action(
    action: dict[str, Any],
    runtime: Any,
    append_message: Any,
    trim_session_histories: Any,
) -> None:
    action_type = str(action.get("type", ""))
    task_id = str(action.get("task_id", ""))
    editing_key = f"editing_task_{task_id}"
    draft_key = f"task_prompt_draft_{task_id}"
    notice: str | None = None

    if action_type == "delete":
        removed = runtime.store.remove_task(task_id)
        notice = f"Task {task_id} was deleted manually." if removed else f"Task {task_id} was not found."
        if removed:
            st.session_state.pop(editing_key, None)
    elif action_type == "run_now":
        result = trigger_task_now(
            executor_agent=runtime.executor_agent,
            store=runtime.store,
            scheduler_loop=runtime.scheduler_loop,
            task_id=task_id,
        )
        if bool(result.get("started")):
            manual_runs = st.session_state.setdefault("manual_run_statuses", {})
            manual_runs[task_id] = {"title": str(result.get("task_title", task_id))}
            st.rerun()
        notice = str(result.get("message", "")).strip() or None
    elif action_type == "set_enabled":
        desired_enabled = bool(action.get("enabled", True))
        updated = runtime.store.set_task_enabled(task_id, desired_enabled)
        if updated:
            if desired_enabled:
                next_run_text = format_task_next_run_html(updated).replace("<strong>", "").replace("</strong>", "")
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
            notice = f"Task {task_id} text updated." if updated else f"Task {task_id} was not found."
            if updated:
                st.session_state[editing_key] = False

    if notice is not None:
        append_message({"role": "assistant", "content": notice, "message_type": "system"})
        trim_session_histories()
        st.rerun()


def send_windows_os_alert(message: str, enabled: bool, timeout_seconds: int) -> None:
    if not enabled:
        return
    try:
        compact = " ".join((message or "").split())
        if not compact:
            return
        compact = compact[:220]
        subprocess.run(
            ["msg", "*", f"/TIME:{timeout_seconds}", compact],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        pass
