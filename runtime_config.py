from __future__ import annotations

import os
from pathlib import Path


STATE_FILE = Path("data") / "state.json"
MAX_SITE_CONTENT_CHARS = 30000  # Approx. 10 A4 pages of plain text.
MAX_CHAT_MESSAGES = max(50, min(int(os.getenv("MAX_CHAT_MESSAGES", "300")), 5000))
MODEL_PROVIDER = (os.getenv("MODEL_PROVIDER", "xai") or "").strip().lower()

MODEL_TIMEOUT_SECONDS = max(10.0, min(float(os.getenv("MODEL_TIMEOUT_SECONDS", "45")), 300.0))
MODEL_MAX_RETRIES = max(0, min(int(os.getenv("MODEL_MAX_RETRIES", "1")), 5))
MODEL_TEMPERATURE = max(0.0, min(float(os.getenv("MODEL_TEMPERATURE", "0.0")), 2.0))

SEARCH_TIMEOUT_SECONDS = max(5.0, min(float(os.getenv("SEARCH_TIMEOUT_SECONDS", "60")), 180.0))
SEARCH_MAX_RETRIES = max(0, min(int(os.getenv("SEARCH_MAX_RETRIES", "2")), 5))

SCHEDULED_TASK_TIMEOUT_SECONDS = max(
    15.0, min(float(os.getenv("SCHEDULED_TASK_TIMEOUT_SECONDS", "90")), 900.0)
)
SCHEDULER_MISFIRE_GRACE_SECONDS = max(
    5, min(int(os.getenv("SCHEDULER_MISFIRE_GRACE_SECONDS", "300")), 3600)
)
FAILED_TASK_RETRY_SECONDS = max(15, min(int(os.getenv("FAILED_TASK_RETRY_SECONDS", "60")), 3600))
