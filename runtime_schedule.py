from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Tuple
from zoneinfo import ZoneInfo

from croniter import croniter


DAY_TO_CRON = {
    "sun": "0",
    "mon": "1",
    "tue": "2",
    "wed": "3",
    "thu": "4",
    "fri": "5",
    "sat": "6",
}


def parse_schedule_definition(schedule_text: str, tz: ZoneInfo) -> Dict[str, Any]:
    raw = schedule_text.strip()
    lowered = raw.lower()

    once_match = re.fullmatch(r"once\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", lowered)
    if once_match:
        date_part = once_match.group(1)
        hour, minute = parse_hhmm(once_match.group(2))
        year, month, day = map(int, date_part.split("-"))
        try:
            local_run_dt = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError as exc:
            raise ValueError(f"Invalid one-time date/time: {exc}") from exc

        now_local = datetime.now(timezone.utc).astimezone(tz)
        if local_run_dt <= now_local:
            raise ValueError("One-time schedule must be in the future for the selected timezone.")

        return {
            "task_type": "once",
            "cron_expr": None,
            "next_run_utc": local_run_dt.astimezone(timezone.utc),
        }

    cron_expr = parse_schedule_to_cron(raw)
    return {
        "task_type": "recurring",
        "cron_expr": cron_expr,
        "next_run_utc": get_next_run_utc(cron_expr, tz),
    }


def parse_schedule_to_cron(schedule_text: str) -> str:
    raw = schedule_text.strip()
    lowered = raw.lower()

    if lowered.startswith("cron:"):
        cron_expr = raw.split(":", 1)[1].strip()
        if not croniter.is_valid(cron_expr):
            raise ValueError(f"Invalid cron expression: {cron_expr}")
        return cron_expr

    every_match = re.fullmatch(r"every\s+(\d+)\s+minutes?", lowered)
    if every_match:
        minutes = int(every_match.group(1))
        if minutes < 1 or minutes > 720:
            raise ValueError("Minutes for 'every N minutes' must be in [1, 720].")
        return f"*/{minutes} * * * *"

    daily_match = re.fullmatch(r"daily\s+(\d{1,2}:\d{2})", lowered)
    if daily_match:
        hour, minute = parse_hhmm(daily_match.group(1))
        return f"{minute} {hour} * * *"

    weekly_match = re.fullmatch(r"weekly\s+([a-z]{3})\s+(\d{1,2}:\d{2})", lowered)
    if weekly_match:
        day = weekly_match.group(1)
        hour, minute = parse_hhmm(weekly_match.group(2))
        cron_day = DAY_TO_CRON.get(day)
        if cron_day is None:
            raise ValueError("Invalid weekly day. Use mon,tue,wed,thu,fri,sat,sun.")
        return f"{minute} {hour} * * {cron_day}"

    days_match = re.fullmatch(r"days\s+([a-z,\s]+)\s+(\d{1,2}:\d{2})", lowered)
    if days_match:
        raw_days = [d.strip() for d in days_match.group(1).split(",") if d.strip()]
        cron_days = []
        for day in raw_days:
            if day not in DAY_TO_CRON:
                raise ValueError("Invalid day in days list. Use mon,tue,wed,thu,fri,sat,sun.")
            cron_days.append(DAY_TO_CRON[day])
        if not cron_days:
            raise ValueError("No days provided for days schedule.")
        hour, minute = parse_hhmm(days_match.group(2))
        return f"{minute} {hour} * * {','.join(cron_days)}"

    raise ValueError(
        "Unsupported schedule format. Use: once YYYY-MM-DD HH:MM | daily HH:MM | weekly DAY HH:MM | "
        "days DAY1,DAY2 HH:MM | every N minutes | cron: M H DOM MON DOW"
    )


def parse_hhmm(hhmm: str) -> Tuple[int, int]:
    h_str, m_str = hhmm.split(":", 1)
    hour, minute = int(h_str), int(m_str)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("Time must be HH:MM in 24-hour format.")
    return hour, minute


def get_next_run_utc(cron_expr: str, tz: ZoneInfo, base_utc: datetime | None = None) -> datetime:
    base = (base_utc or datetime.now(timezone.utc)).astimezone(tz)
    return croniter(cron_expr, base).get_next(datetime).astimezone(timezone.utc)

