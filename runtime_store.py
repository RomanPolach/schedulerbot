from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from runtime_config import MAX_CHAT_MESSAGES
from runtime_schedule import get_next_run_utc, parse_schedule_definition


def utc_now_iso() -> str:
    return dt_to_iso(datetime.now(timezone.utc))


def dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def format_cz_datetime(utc_iso: str | None, timezone_name: str = "UTC") -> str:
    if not utc_iso:
        return "-"
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except Exception:
        tz = timezone.utc

    try:
        raw = str(utc_iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(tz)
        return f"{local.day}. {local.month}. {local.year} - {local.strftime('%H:%M')}"
    except Exception:
        return str(utc_iso)


def format_task_table(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return "No scheduled tasks."

    lines = []
    for task in tasks:
        status = "enabled" if task.get("enabled", True) else "paused"
        tz_name = str(task.get("timezone", "UTC") or "UTC")
        next_run_local = format_cz_datetime(task.get("next_run_utc", "-"), tz_name)
        lines.append(
            " | ".join(
                [
                    f"id={task['id']}",
                    f"status={status}",
                    f"schedule={task['schedule_text']}",
                    f"next_run={next_run_local}",
                    f"timezone={tz_name}",
                    f"task={task['task_prompt']}",
                ]
            )
        )
    return "\n".join(lines)


class StateStore:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.file_path.exists():
            self._write_state(
                {
                    "tasks": [],
                    "events": [],
                    "next_event_id": 1,
                    "chat_messages": [],
                    "processed_event_id": 0,
                }
            )
        else:
            with self._lock:
                state = self._normalize_state(self._read_state())
                self._write_state(state)

    @staticmethod
    def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
        state.setdefault("tasks", [])
        state.setdefault("events", [])
        state.setdefault("next_event_id", 1)
        state.setdefault("chat_messages", [])
        state.setdefault("processed_event_id", 0)
        return state

    def _read_state(self) -> Dict[str, Any]:
        # Accept BOM-prefixed JSON files written by some Windows tooling.
        with self.file_path.open("r", encoding="utf-8-sig") as f:
            return self._normalize_state(json.load(f))

    def _write_state(self, state: Dict[str, Any]) -> None:
        with self.file_path.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def add_task(self, task_prompt: str, schedule_text: str, timezone_name: str) -> Dict[str, Any]:
        tz = ZoneInfo(timezone_name)
        schedule_meta = parse_schedule_definition(schedule_text, tz)

        task = {
            "id": str(uuid.uuid4())[:8],
            "task_prompt": task_prompt,
            "schedule_text": schedule_text,
            "task_type": schedule_meta["task_type"],
            "cron_expr": schedule_meta["cron_expr"],
            "timezone": timezone_name,
            "created_at": utc_now_iso(),
            "next_run_utc": dt_to_iso(schedule_meta["next_run_utc"]),
            "last_run_utc": None,
            "last_result": None,
            "enabled": True,
        }

        with self._lock:
            state = self._read_state()
            state["tasks"].append(task)
            self._write_state(state)
        return task

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._read_state()["tasks"])

    def remove_task(self, task_id: str) -> bool:
        with self._lock:
            state = self._read_state()
            before = len(state["tasks"])
            state["tasks"] = [t for t in state["tasks"] if t["id"] != task_id]
            removed = len(state["tasks"]) < before
            if removed:
                self._write_state(state)
            return removed

    def set_task_enabled(self, task_id: str, enabled: bool) -> Dict[str, Any] | None:
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            state = self._read_state()
            for task in state["tasks"]:
                if task["id"] != task_id:
                    continue

                task["enabled"] = bool(enabled)
                # When resuming recurring tasks, recompute from now to avoid stale immediate runs.
                if task["enabled"] and task.get("task_type") != "once" and task.get("cron_expr"):
                    tz = ZoneInfo(task["timezone"])
                    task["next_run_utc"] = dt_to_iso(get_next_run_utc(task["cron_expr"], tz, base_utc=now_utc))

                self._write_state(state)
                return dict(task)
        return None

    def update_task_prompt(self, task_id: str, task_prompt: str) -> Dict[str, Any] | None:
        cleaned = (task_prompt or "").strip()
        if not cleaned:
            return None

        with self._lock:
            state = self._read_state()
            for task in state["tasks"]:
                if task["id"] != task_id:
                    continue

                task["task_prompt"] = cleaned
                self._write_state(state)
                return dict(task)
        return None

    def get_due_tasks(self, now_utc: datetime) -> List[Dict[str, Any]]:
        due: List[Dict[str, Any]] = []
        with self._lock:
            state = self._read_state()
            for task in state["tasks"]:
                if not task.get("enabled", True):
                    continue
                next_run_utc = task.get("next_run_utc")
                if not next_run_utc:
                    continue
                next_run = iso_to_dt(next_run_utc)
                if next_run <= now_utc:
                    due.append(task)
        return due

    def mark_task_run(self, task_id: str, result: str, now_utc: datetime) -> None:
        with self._lock:
            state = self._read_state()
            for index, task in enumerate(state["tasks"]):
                if task["id"] != task_id:
                    continue

                task_type = task.get("task_type", "recurring")
                if task_type == "once":
                    # One-time tasks are removed after first successful execution.
                    state["tasks"].pop(index)
                    break

                tz = ZoneInfo(task["timezone"])
                task["last_run_utc"] = dt_to_iso(now_utc)
                task["last_result"] = result
                task["next_run_utc"] = dt_to_iso(get_next_run_utc(task["cron_expr"], tz, base_utc=now_utc))
                break
            self._write_state(state)

    def mark_task_retry(
        self, task_id: str, result: str, now_utc: datetime, retry_after_seconds: int
    ) -> None:
        retry_after_seconds = max(15, min(int(retry_after_seconds), 3600))
        with self._lock:
            state = self._read_state()
            for task in state["tasks"]:
                if task["id"] != task_id:
                    continue

                task["last_run_utc"] = dt_to_iso(now_utc)
                task["last_result"] = result
                task["next_run_utc"] = dt_to_iso(now_utc + timedelta(seconds=retry_after_seconds))
                break
            self._write_state(state)

    def add_event(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            state = self._read_state()
            event = {
                "id": state["next_event_id"],
                "created_at": utc_now_iso(),
                **payload,
            }
            state["next_event_id"] += 1
            state["events"].append(event)
            # Keep only latest 500 events.
            state["events"] = state["events"][-500:]
            self._write_state(state)

    def get_events_after(self, event_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            events = self._read_state()["events"]
            return [e for e in events if e["id"] > event_id]

    def get_chat_messages(self) -> List[Dict[str, Any]]:
        with self._lock:
            state = self._read_state()
            return list(state.get("chat_messages", []))

    def append_chat_message(
        self,
        role: str,
        content: str,
        tool_calls: List[Dict[str, Any]] | None = None,
        message_type: str | None = None,
    ) -> None:
        with self._lock:
            state = self._read_state()
            message: Dict[str, Any] = {"role": role, "content": content}
            if tool_calls is not None:
                message["tool_calls"] = tool_calls
            if message_type:
                message["message_type"] = str(message_type)
            state["chat_messages"].append(message)
            # Keep a bounded chat history to prevent unbounded state growth.
            state["chat_messages"] = state["chat_messages"][-MAX_CHAT_MESSAGES:]
            self._write_state(state)

    def clear_chat_messages(self) -> None:
        with self._lock:
            state = self._read_state()
            state["chat_messages"] = []
            self._write_state(state)

    def get_processed_event_id(self) -> int:
        with self._lock:
            state = self._read_state()
            return int(state.get("processed_event_id", 0))

    def set_processed_event_id(self, event_id: int) -> None:
        with self._lock:
            state = self._read_state()
            state["processed_event_id"] = int(event_id)
            self._write_state(state)
