from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from .runtime_config import AGENT_TRACE_FILE, MAX_CHAT_MESSAGES
from .runtime_schedule import get_next_run_utc, parse_schedule_definition


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
                    f"title={task.get('title', '-')}",
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
    _TRACE_TASK_CREATED_RE = re.compile(
        r"Scheduled task created\. id=(?P<task_id>[A-Za-z0-9_-]+), title='(?P<title>[^']+)'"
    )

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.backup_path = self.file_path.with_name(f"{self.file_path.name}.bak")
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
                    "task_registry": {},
                    "ui_presence": {},
                }
            )
        else:
            with self._lock:
                state = self._normalize_state(self._read_state())
                self._write_state(state)

    @staticmethod
    def _fallback_task_title(task: Dict[str, Any]) -> str:
        task_id = str(task.get("id", "")).strip()
        if task_id:
            return f"Legacy task {task_id}"
        return "Legacy task"

    @staticmethod
    def _clean_task_title(value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _is_meaningful_task_title(task_id: Any, title: Any) -> bool:
        clean_task_id = StateStore._clean_task_title(task_id)
        clean_title = StateStore._clean_task_title(title)
        return bool(clean_title and clean_title != clean_task_id)

    @staticmethod
    def _repair_task_title(task_id: Any, raw_title: Any, registry: Dict[str, str]) -> str:
        clean_task_id = StateStore._clean_task_title(task_id)
        clean_title = StateStore._clean_task_title(raw_title)
        if StateStore._is_meaningful_task_title(clean_task_id, clean_title):
            return clean_title
        remembered_title = StateStore._clean_task_title(registry.get(clean_task_id, ""))
        if StateStore._is_meaningful_task_title(clean_task_id, remembered_title):
            return remembered_title
        return clean_title or clean_task_id

    @classmethod
    def _extract_task_registry_from_trace_file(cls, trace_path: Path) -> Dict[str, str]:
        registry: Dict[str, str] = {}
        if not trace_path.exists():
            return registry

        try:
            with trace_path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    match = cls._TRACE_TASK_CREATED_RE.search(line)
                    if not match:
                        continue
                    task_id = cls._clean_task_title(match.group("task_id"))
                    title = cls._clean_task_title(match.group("title"))
                    if cls._is_meaningful_task_title(task_id, title):
                        registry[task_id] = title
        except Exception:
            return {}

        return registry

    @staticmethod
    def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
        state.setdefault("tasks", [])
        state.setdefault("events", [])
        state.setdefault("next_event_id", 1)
        state.setdefault("chat_messages", [])
        state.setdefault("processed_event_id", 0)
        state.setdefault("task_registry", {})
        state.setdefault("ui_presence", {})

        task_registry: Dict[str, str] = {}
        for raw_task_id, raw_title in dict(state.get("task_registry", {})).items():
            task_id = StateStore._clean_task_title(raw_task_id)
            title = StateStore._clean_task_title(raw_title)
            if StateStore._is_meaningful_task_title(task_id, title):
                task_registry[task_id] = title

        normalized_tasks: List[Dict[str, Any]] = []
        for task in state.get("tasks", []):
            if not isinstance(task, dict):
                continue
            normalized_task = dict(task)
            task_id = StateStore._clean_task_title(normalized_task.get("id", ""))
            title = StateStore._clean_task_title(normalized_task.get("title", ""))
            if not title:
                normalized_task["title"] = StateStore._fallback_task_title(normalized_task)
            else:
                normalized_task["title"] = title
            clean_title = StateStore._clean_task_title(normalized_task.get("title", ""))
            if StateStore._is_meaningful_task_title(task_id, clean_title):
                task_registry[task_id] = clean_title
            normalized_tasks.append(normalized_task)
        state["tasks"] = normalized_tasks

        normalized_events: List[Dict[str, Any]] = []
        unresolved_task_ids: set[str] = set()
        for event in state.get("events", []):
            if not isinstance(event, dict):
                continue
            normalized_event = dict(event)
            task_id = StateStore._clean_task_title(normalized_event.get("task_id", ""))
            title = StateStore._clean_task_title(normalized_event.get("task_title", ""))
            if task_id and StateStore._is_meaningful_task_title(task_id, title):
                task_registry[task_id] = title
            elif task_id:
                unresolved_task_ids.add(task_id)
            normalized_events.append(normalized_event)
        state["events"] = normalized_events

        normalized_messages: List[Dict[str, Any]] = []
        seen_scheduled_event_ids: set[str] = set()
        for message in state.get("chat_messages", []):
            if not isinstance(message, dict):
                continue
            normalized_message = dict(message)
            message_type = str(normalized_message.get("message_type", "")).strip().lower()
            scheduled_event_id = str(normalized_message.get("scheduled_event_id", "")).strip()
            task_id = StateStore._clean_task_title(normalized_message.get("task_id", ""))
            task_title = StateStore._clean_task_title(normalized_message.get("task_title", ""))
            if task_id and StateStore._is_meaningful_task_title(task_id, task_title):
                task_registry[task_id] = task_title
            elif task_id:
                unresolved_task_ids.add(task_id)
            if message_type == "scheduled" and scheduled_event_id:
                if "scheduled_read" not in normalized_message:
                    # Legacy messages are treated as already read.
                    normalized_message["scheduled_read"] = True
                if scheduled_event_id in seen_scheduled_event_ids:
                    continue
                seen_scheduled_event_ids.add(scheduled_event_id)
            normalized_messages.append(normalized_message)

        if unresolved_task_ids:
            trace_registry = StateStore._extract_task_registry_from_trace_file(AGENT_TRACE_FILE)
            for task_id in unresolved_task_ids:
                trace_title = StateStore._clean_task_title(trace_registry.get(task_id, ""))
                if StateStore._is_meaningful_task_title(task_id, trace_title):
                    task_registry[task_id] = trace_title

        for event in state["events"]:
            if not isinstance(event, dict):
                continue
            task_id = StateStore._clean_task_title(event.get("task_id", ""))
            if task_id:
                event["task_title"] = StateStore._repair_task_title(
                    task_id,
                    event.get("task_title", ""),
                    task_registry,
                )

        repaired_messages: List[Dict[str, Any]] = []
        for message in normalized_messages:
            if not isinstance(message, dict):
                continue
            task_id = StateStore._clean_task_title(message.get("task_id", ""))
            if task_id:
                message["task_title"] = StateStore._repair_task_title(
                    task_id,
                    message.get("task_title", ""),
                    task_registry,
                )
            repaired_messages.append(message)

        state["task_registry"] = task_registry
        normalized_ui_presence: Dict[str, Dict[str, Any]] = {}
        for raw_session_id, raw_presence in dict(state.get("ui_presence", {})).items():
            session_id = StateStore._clean_task_title(raw_session_id)
            if not session_id or not isinstance(raw_presence, dict):
                continue
            normalized_ui_presence[session_id] = {
                "watching": bool(raw_presence.get("watching", False)),
                "visible": bool(raw_presence.get("visible", False)),
                "focused": bool(raw_presence.get("focused", False)),
                "updated_at": str(raw_presence.get("updated_at", "") or ""),
            }
        state["ui_presence"] = normalized_ui_presence
        state["chat_messages"] = repaired_messages[-MAX_CHAT_MESSAGES:]
        return state

    def _read_state(self) -> Dict[str, Any]:
        try:
            state = self._load_json_file(self.file_path)
        except json.JSONDecodeError:
            # If the primary file is truncated/corrupted, recover from backup.
            if self.backup_path.exists():
                state = self._load_json_file(self.backup_path)
                self._write_state(state)
            else:
                raise
        return self._normalize_state(state)

    def _load_json_file(self, path: Path) -> Dict[str, Any]:
        # Accept BOM-prefixed JSON files written by some Windows tooling.
        with path.open("r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError(f"State file {path} must contain a JSON object.")
        return loaded

    def _write_state(self, state: Dict[str, Any]) -> None:
        normalized_state = self._normalize_state(state)
        temp_path = self.file_path.with_name(f"{self.file_path.name}.tmp")

        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(normalized_state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        if self.file_path.exists():
            shutil.copy2(self.file_path, self.backup_path)

        os.replace(temp_path, self.file_path)

    def add_task(self, title: str, task_prompt: str, schedule_text: str, timezone_name: str) -> Dict[str, Any]:
        clean_title = " ".join((title or "").split()).strip()
        if not clean_title:
            raise ValueError("Task title cannot be empty.")

        tz = ZoneInfo(timezone_name)
        schedule_meta = parse_schedule_definition(schedule_text, tz)

        task = {
            "id": str(uuid.uuid4())[:8],
            "title": clean_title,
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

    def resolve_task_title(self, task_id: str, fallback: str = "") -> str:
        clean_task_id = self._clean_task_title(task_id)
        clean_fallback = self._clean_task_title(fallback)
        if not clean_task_id:
            return clean_fallback

        with self._lock:
            state = self._read_state()
            for task in state.get("tasks", []):
                if self._clean_task_title(task.get("id", "")) != clean_task_id:
                    continue
                title = self._clean_task_title(task.get("title", ""))
                if self._is_meaningful_task_title(clean_task_id, title):
                    return title

            remembered_title = self._clean_task_title(state.get("task_registry", {}).get(clean_task_id, ""))
            if self._is_meaningful_task_title(clean_task_id, remembered_title):
                return remembered_title

        return clean_fallback or clean_task_id

    def update_ui_presence(
        self,
        session_id: str,
        *,
        watching: bool,
        visible: bool,
        focused: bool,
        now_utc: datetime | None = None,
    ) -> None:
        clean_session_id = self._clean_task_title(session_id)
        if not clean_session_id:
            return

        if now_utc is None:
            now_utc = datetime.now(timezone.utc)

        with self._lock:
            state = self._read_state()
            ui_presence = dict(state.get("ui_presence", {}))
            cutoff = now_utc - timedelta(minutes=10)
            pruned_ui_presence: Dict[str, Dict[str, Any]] = {}
            for existing_session_id, payload in ui_presence.items():
                if not isinstance(payload, dict):
                    continue
                updated_at = iso_to_dt(str(payload.get("updated_at", "") or "")) if str(payload.get("updated_at", "")).strip() else None
                if updated_at is not None and updated_at >= cutoff:
                    pruned_ui_presence[str(existing_session_id)] = payload

            pruned_ui_presence[clean_session_id] = {
                "watching": bool(watching),
                "visible": bool(visible),
                "focused": bool(focused),
                "updated_at": dt_to_iso(now_utc),
            }
            state["ui_presence"] = pruned_ui_presence
            self._write_state(state)

    def has_recently_watched_ui(self, within_seconds: float = 5.0) -> bool:
        freshness_seconds = max(1.0, min(float(within_seconds), 300.0))
        now_utc = datetime.now(timezone.utc)

        with self._lock:
            state = self._read_state()
            for payload in state.get("ui_presence", {}).values():
                if not isinstance(payload, dict):
                    continue
                if not bool(payload.get("watching", False)):
                    continue
                updated_at_raw = str(payload.get("updated_at", "") or "").strip()
                if not updated_at_raw:
                    continue
                try:
                    updated_at = iso_to_dt(updated_at_raw)
                except Exception:
                    continue
                if (now_utc - updated_at).total_seconds() <= freshness_seconds:
                    return True
        return False

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
        extra_fields: Dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            state = self._read_state()
            message: Dict[str, Any] = {"role": role, "content": content}
            if tool_calls is not None:
                message["tool_calls"] = tool_calls
            if message_type:
                message["message_type"] = str(message_type)
            if isinstance(extra_fields, dict):
                for key, value in extra_fields.items():
                    if value is not None:
                        message[str(key)] = value
            if str(message.get("message_type", "")).strip().lower() == "scheduled":
                scheduled_event_id = str(message.get("scheduled_event_id", "")).strip()
                if scheduled_event_id:
                    for existing in state.get("chat_messages", []):
                        if not isinstance(existing, dict):
                            continue
                        existing_type = str(existing.get("message_type", "")).strip().lower()
                        existing_event_id = str(existing.get("scheduled_event_id", "")).strip()
                        if existing_type == "scheduled" and existing_event_id == scheduled_event_id:
                            return
            state["chat_messages"].append(message)
            # Keep a bounded chat history to prevent unbounded state growth.
            state["chat_messages"] = state["chat_messages"][-MAX_CHAT_MESSAGES:]
            self._write_state(state)

    def clear_chat_messages(self) -> None:
        with self._lock:
            state = self._read_state()
            state["chat_messages"] = []
            self._write_state(state)

    def mark_scheduled_message_read(self, scheduled_event_id: str) -> bool:
        target_event_id = str(scheduled_event_id or "").strip()
        if not target_event_id:
            return False

        with self._lock:
            state = self._read_state()
            updated = False
            for message in state.get("chat_messages", []):
                if not isinstance(message, dict):
                    continue
                message_type = str(message.get("message_type", "")).strip().lower()
                event_id = str(message.get("scheduled_event_id", "")).strip()
                if message_type == "scheduled" and event_id == target_event_id:
                    if bool(message.get("scheduled_read", False)):
                        return False
                    message["scheduled_read"] = True
                    updated = True
                    break
            if updated:
                self._write_state(state)
            return updated

    def get_processed_event_id(self) -> int:
        with self._lock:
            state = self._read_state()
            return int(state.get("processed_event_id", 0))

    def set_processed_event_id(self, event_id: int) -> None:
        with self._lock:
            state = self._read_state()
            state["processed_event_id"] = int(event_id)
            self._write_state(state)
