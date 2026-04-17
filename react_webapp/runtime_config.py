from __future__ import annotations

import os
from pathlib import Path


RUNTIME_BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = Path(os.getenv("STATE_FILE", str(RUNTIME_BASE_DIR / "data" / "state.json")))
MAX_SITE_CONTENT_CHARS = 20000  # Approx. 6-7 A4 pages of plain text.
MAX_CHAT_MESSAGES = max(50, min(int(os.getenv("MAX_CHAT_MESSAGES", "300")), 5000))
MODEL_PROVIDER = "gemini"

MODEL_TIMEOUT_SECONDS = max(10.0, min(float(os.getenv("MODEL_TIMEOUT_SECONDS", "45")), 300.0))
MODEL_MAX_RETRIES = max(0, min(int(os.getenv("MODEL_MAX_RETRIES", "1")), 5))
MODEL_TEMPERATURE = max(0.0, min(float(os.getenv("MODEL_TEMPERATURE", "0.0")), 2.0))

GEMINI_INCLUDE_THOUGHTS = (os.getenv("GEMINI_INCLUDE_THOUGHTS", "true") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GEMINI_THINKING_LEVEL = (os.getenv("GEMINI_THINKING_LEVEL", "low") or "").strip().lower()
if GEMINI_THINKING_LEVEL not in {"minimal", "low", "medium", "high"}:
    GEMINI_THINKING_LEVEL = "low"

_gemini_thinking_budget_raw = (os.getenv("GEMINI_THINKING_BUDGET", "") or "").strip()
GEMINI_THINKING_BUDGET = int(_gemini_thinking_budget_raw) if _gemini_thinking_budget_raw else None

SEARCH_TIMEOUT_SECONDS = max(5.0, min(float(os.getenv("SEARCH_TIMEOUT_SECONDS", "60")), 180.0))
SEARCH_MAX_RETRIES = max(0, min(int(os.getenv("SEARCH_MAX_RETRIES", "2")), 5))

SCHEDULED_TASK_TIMEOUT_SECONDS = max(
    15.0, min(float(os.getenv("SCHEDULED_TASK_TIMEOUT_SECONDS", "90")), 900.0)
)
SCHEDULER_MISFIRE_GRACE_SECONDS = max(
    5, min(int(os.getenv("SCHEDULER_MISFIRE_GRACE_SECONDS", "300")), 3600)
)
FAILED_TASK_RETRY_SECONDS = max(15, min(int(os.getenv("FAILED_TASK_RETRY_SECONDS", "60")), 3600))

AGENT_TRACE_ENABLED = (os.getenv("AGENT_TRACE_ENABLED", "true") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AGENT_TRACE_FILE = Path(
    os.getenv("AGENT_TRACE_FILE", str(RUNTIME_BASE_DIR / "data" / "agent_trace.log"))
)
AGENT_TRACE_MAX_TEXT_CHARS = max(500, min(int(os.getenv("AGENT_TRACE_MAX_TEXT_CHARS", "8000")), 200000))
AGENT_TRACE_FORMAT = (os.getenv("AGENT_TRACE_FORMAT", "pretty") or "").strip().lower()
if AGENT_TRACE_FORMAT not in {"pretty", "jsonl"}:
    AGENT_TRACE_FORMAT = "pretty"

AGENT_GRAPH_DEBUG = (os.getenv("AGENT_GRAPH_DEBUG", "false") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

MAX_TOOL_CALLS_PER_RUN = max(1, min(int(os.getenv("MAX_TOOL_CALLS_PER_RUN", "10")), 100))
