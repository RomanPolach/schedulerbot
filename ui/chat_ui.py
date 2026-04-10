from __future__ import annotations

import json
from html import escape
from typing import Any

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage

from runtime_store import format_cz_datetime


def get_user_timezone_name() -> str:
    from datetime import datetime
    import os

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
                st.code(_format_tool_args(args), language=_tool_args_language(args))


def should_render_tool_calls(message: dict[str, Any]) -> bool:
    if message.get("role") != "assistant":
        return False

    message_type = str(message.get("message_type", "") or "").strip().lower()
    if message_type:
        return message_type == "ai"

    return "tool_calls" in message


def is_scheduled_result_message(message: dict[str, Any]) -> bool:
    return str(message.get("message_type", "") or "").strip().lower() == "scheduled"


def scheduled_result_status_text(message: dict[str, Any]) -> str:
    status = str(message.get("scheduled_status", "") or "").strip().lower()
    created_at_text = format_event_timestamp(message.get("scheduled_created_at"))
    if status == "retry_scheduled":
        return f"Retry planned - {created_at_text}"
    if status == "failed_no_retry":
        return f"Failed, no retry - {created_at_text}"
    return f"Completed - {created_at_text}"


def render_scheduled_result_message(message: dict[str, Any], index: int) -> None:
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
    key_suffix = f"{scheduled_event_id}_{index}"

    with st.container(border=False, key=f"scheduled_result_{container_state}_{key_suffix}"):
        with st.expander(expander_label, expanded=False):
            meta_bits = []
            task_id = str(message.get("task_id", "") or "").strip()
            if task_id:
                meta_bits.append(f"Task ID: `{task_id}`")
            meta_bits.append(status_text)
            st.caption(" - ".join(meta_bits))
            if is_unread and st.button("Mark as read", key=f"mark_scheduled_read_{key_suffix}"):
                unread_ids.discard(scheduled_event_id)
                st.rerun()
            st.markdown(str(message.get("content", "") or ""))
            render_tool_calls_panel(message.get("tool_calls"))


def to_agent_messages(chat_messages: list[dict[str, Any]]) -> list:
    converted = []
    for message in chat_messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "user":
            converted.append(HumanMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
    return converted


def is_legacy_intro_message(message: dict[str, Any], legacy_intro_message: str) -> bool:
    if not isinstance(message, dict):
        return False
    return (
        str(message.get("role", "")).strip().lower() == "assistant"
        and str(message.get("message_type", "")).strip().lower() == "system"
        and str(message.get("content", "")).strip() == legacy_intro_message
    )


def render_page_header(target: Any, subtitle: str, has_messages: bool) -> None:
    with target.container():
        st.title("Schedule Assistant")
        if not has_messages:
            st.markdown(
                f'<div class="empty-chat-subtitle">{escape(subtitle)}</div>',
                unsafe_allow_html=True,
            )


def render_manual_run_statuses(manual_runs: dict[str, dict[str, str]] | None) -> None:
    if not isinstance(manual_runs, dict) or not manual_runs:
        return

    for task_id, payload in manual_runs.items():
        if not isinstance(payload, dict):
            continue
        task_title = " ".join(str(payload.get("title", "")).split()).strip() or task_id
        status_box = st.status(
            f'Running task "{task_title}" manually',
            expanded=False,
            state="running",
        )
        status_box.write("Waiting for the result to come back.")


def _tool_args_language(args: Any) -> str:
    if isinstance(args, dict):
        return "yaml"
    if isinstance(args, list):
        return "json"
    return "text"


def _format_tool_args(args: Any) -> str:
    if isinstance(args, dict):
        lines: list[str] = []
        for key, value in args.items():
            formatted_value = _format_tool_arg_value(value)
            if "\n" in formatted_value:
                indented = "\n".join(f"  {line}" for line in formatted_value.splitlines())
                lines.append(f"{key}:")
                lines.append(indented)
            else:
                lines.append(f"{key}: {formatted_value}")
        return "\n".join(lines) if lines else "(no arguments)"

    if isinstance(args, list):
        return json.dumps(args, ensure_ascii=False, indent=2)

    if args is None:
        return "(no arguments)"

    return str(args)


def _format_tool_arg_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, (int, float, bool)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, indent=2)
