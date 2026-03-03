# Schedule Chatbot (LangChain v1 Agent + Streamlit)

This project implements a **reactive + proactive** chatbot:

- Reactive: answers normal chat questions.
- Proactive: executes scheduled tasks on a configurable poll interval and posts results back into chat.

It uses **LangChain v1.0 agent API** (`create_agent`) and a Streamlit chat UI.

## Features

- LangChain v1 agent with tool-calling.
- Streamlit chatbot interface.
- Recurring scheduler (`APScheduler`) that checks due tasks on a configurable interval.
- Sidebar task management: pause/resume tasks, edit task text, and delete tasks.
- Tools:
  - `web_search`: web search via LangChain `TavilySearch`, returns source URLs/snippets (optional short model answer).
  - `parse_websites`: fetch + parse multiple website URLs.
  - `open_file`: read local TXT/Word/Excel/OpenDocument files and return extracted content in markdown.
  - `run_windows_cmd`: run Windows `cmd /c` commands.
  - `schedule_task`, `list_scheduled_tasks`, `remove_scheduled_task`.
- Persistent state in `data/state.json`.

## Requirements

- Python 3.10+
- Chat model provider API key:
  - xAI/Grok: `XAI_API_KEY`
  - Google Gemini: `GOOGLE_API_KEY`
- `TAVILY_API_KEY` set (for `web_search`)

Create and activate a virtual environment (recommended):

```bash
python -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

Optional env vars:

- `MODEL_PROVIDER` (default: `xai`, options: `xai|gemini`)
- `AGENT_MODEL` (default: `grok-4-1-fast-non-reasoning`; Gemini 3 Flash example: `gemini-3-flash-preview`)
- `XAI_BASE_URL` (default: `https://api.x.ai/v1`)
- `XAI_API_KEY` (required when `MODEL_PROVIDER=xai`)
- `GOOGLE_API_KEY` (required when `MODEL_PROVIDER=gemini`)
- `MODEL_TEMPERATURE` (default: `0.0`, range `0.0..2.0`, lower = more consistent)
- `TAVILY_API_KEY` (required for `web_search`)
- `WEB_SEARCH_MAX_RESULTS` (default: `6`, range `1..10`)
- `TAVILY_SEARCH_DEPTH` (default: `advanced`, options: `basic|advanced`)
- `TAVILY_TOPIC` (default: `general`, options: `general|news|finance`)
- `TAVILY_TIME_RANGE` (optional, e.g. `day|week|month|year`)
- `TAVILY_INCLUDE_DOMAINS` (optional CSV list of domains)
- `TAVILY_EXCLUDE_DOMAINS` (optional CSV list of domains)
- `AUTO_REFRESH_MS` (default: `30000`)
- `SCHEDULER_POLL_SECONDS` (default: `60`)
- `PLAYWRIGHT_REUSE_BROWSER` (default: `true`, keeps Chromium alive and reuses it)
- `PLAYWRIGHT_STARTUP_TIMEOUT_SECONDS` (default: `30`, startup warmup timeout)
- `PLAYWRIGHT_MAX_CONCURRENCY` (default: `4`, range `1..8` for `parse_websites`)
- `PLAYWRIGHT_RENDER_WAIT_MS` (default: `350`, extra wait after navigation)
- `PLAYWRIGHT_PAGE_TIMEOUT_SECONDS` (default: `30`, per-page timeout)

## Schedule Formats

The scheduling tool supports:

- `daily HH:MM`
- `weekly DAY HH:MM` (DAY: `mon,tue,wed,thu,fri,sat,sun`)
- `days DAY1,DAY2 HH:MM` (example: `days mon,wed,fri 09:30`)
- `every N minutes`
- `cron: M H DOM MON DOW`

Timezone is provided separately (examples: `UTC`, `America/New_York`).

## Example prompts

- `What are the top AI headlines today?`
- `Schedule a daily 09:00 summary of AI news for me in America/New_York.`
- `Schedule every 30 minutes to parse websites: cnn.com, bbc.com and summarize.`
- `Run this command and explain output: dir C:\\`
- `List my scheduled tasks.`
- `Remove scheduled task ab12cd34`

## Notes

- `run_windows_cmd` executes commands on the local machine. Use carefully.
- Scheduler poll interval is configurable via `SCHEDULER_POLL_SECONDS` (default 60s).
