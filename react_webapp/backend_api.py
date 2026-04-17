from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .runtime import (
    create_runtime,
    invoke_agent_detailed,
    invoke_agent_streaming_detailed,
    trigger_task_now,
)


load_dotenv()

MODEL_NAME = os.getenv("AGENT_MODEL", "gemini-3-flash-preview")
RUNTIME_SCHEMA_VERSION = "2026-04-17-react-webapp-v1"

runtime = create_runtime(model_name=MODEL_NAME)

app = FastAPI(title="Schedule Chatbot API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)


class TaskPatchRequest(BaseModel):
    enabled: Optional[bool] = None
    task_prompt: Optional[str] = None


def _to_agent_messages(chat_messages: List[Dict[str, Any]]) -> List[Any]:
    from langchain_core.messages import AIMessage, HumanMessage

    converted: List[Any] = []
    for message in chat_messages:
        role = str(message.get("role", "")).strip().lower()
        content = str(message.get("content", ""))
        if role == "user":
            converted.append(HumanMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
    return converted


def _known_scheduled_event_ids(chat_messages: List[Dict[str, Any]]) -> set[str]:
    known: set[str] = set()
    for message in chat_messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("message_type", "")).strip().lower() != "scheduled":
            continue
        event_id = str(message.get("scheduled_event_id", "")).strip()
        if event_id:
            known.add(event_id)
    return known


def _ingest_scheduled_events(after_event_id: int) -> tuple[List[Dict[str, Any]], int]:
    new_events = runtime.store.get_events_after(after_event_id)
    if not new_events:
        return [], after_event_id

    chat_messages = runtime.store.get_chat_messages()
    known_event_ids = _known_scheduled_event_ids(chat_messages)

    appended_messages: List[Dict[str, Any]] = []
    for event in new_events:
        if event.get("type") != "scheduled_task_result":
            continue

        scheduled_event_id = str(event.get("id", "")).strip()
        if not scheduled_event_id or scheduled_event_id in known_event_ids:
            continue

        result_text = str(event.get("message", "") or "")
        raw_event_title = " ".join(str(event.get("task_title", "")).split()).strip()
        task_id = str(event.get("task_id", "") or "").strip()
        task_title = runtime.store.resolve_task_title(task_id, fallback=raw_event_title or task_id)

        scheduled_message = {
            "role": "assistant",
            "content": result_text,
            "tool_calls": event.get("tool_calls", []),
            "message_type": "scheduled",
            "scheduled_event_id": scheduled_event_id,
            "scheduled_created_at": event.get("created_at"),
            "scheduled_status": event.get("status", "completed"),
            "scheduled_read": False,
            "task_id": task_id,
            "task_title": task_title or task_id,
        }
        runtime.store.append_chat_message(
            role=scheduled_message["role"],
            content=scheduled_message["content"],
            tool_calls=scheduled_message["tool_calls"],
            message_type=scheduled_message["message_type"],
            extra_fields={
                "scheduled_event_id": scheduled_message["scheduled_event_id"],
                "scheduled_created_at": scheduled_message["scheduled_created_at"],
                "scheduled_status": scheduled_message["scheduled_status"],
                "scheduled_read": False,
                "task_id": scheduled_message["task_id"],
                "task_title": scheduled_message["task_title"],
            },
        )
        appended_messages.append(scheduled_message)
        known_event_ids.add(scheduled_event_id)

    latest_event_id = int(new_events[-1]["id"])
    runtime.store.set_processed_event_id(latest_event_id)
    return appended_messages, latest_event_id


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "model": MODEL_NAME, "schema_version": RUNTIME_SCHEMA_VERSION}


@app.get("/api/bootstrap")
def bootstrap() -> Dict[str, Any]:
    processed_id = runtime.store.get_processed_event_id()
    _ingest_scheduled_events(processed_id)
    return {
        "messages": runtime.store.get_chat_messages(),
        "tasks": runtime.store.list_tasks(),
        "processed_event_id": runtime.store.get_processed_event_id(),
    }


@app.get("/api/poll")
def poll(after: int = 0) -> Dict[str, Any]:
    new_messages, last_event_id = _ingest_scheduled_events(after)
    return {
        "new_messages": new_messages,
        "last_event_id": last_event_id,
        "tasks": runtime.store.list_tasks(),
    }


@app.post("/api/chat")
def chat(req: ChatRequest) -> Dict[str, Any]:
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")

    runtime.store.append_chat_message(role="user", content=prompt)

    try:
        agent_messages = _to_agent_messages(runtime.store.get_chat_messages())
        detailed = invoke_agent_detailed(runtime.agent, agent_messages)
        response = detailed["text"]
        response_tool_calls = detailed.get("tool_calls", [])
    except Exception as exc:
        response = f"Agent error: {exc}"
        response_tool_calls = []

    runtime.store.append_chat_message(
        role="assistant",
        content=response,
        tool_calls=response_tool_calls,
        message_type="ai",
    )

    assistant_message = {
        "role": "assistant",
        "content": response,
        "tool_calls": response_tool_calls,
        "message_type": "ai",
    }
    return {
        "assistant_message": assistant_message,
        "tasks": runtime.store.list_tasks(),
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")

    runtime.store.append_chat_message(role="user", content=prompt)

    event_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(payload: Dict[str, Any]) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, payload)

    def worker() -> None:
        try:
            agent_messages = _to_agent_messages(runtime.store.get_chat_messages())

            def on_text(partial_text: str) -> None:
                emit({"type": "partial", "text": str(partial_text or "")})

            detailed = invoke_agent_streaming_detailed(
                runtime.agent,
                agent_messages,
                on_text=on_text,
            )
            response = detailed["text"]
            response_tool_calls = detailed.get("tool_calls", [])
        except Exception as exc:
            response = f"Agent error: {exc}"
            response_tool_calls = []
            runtime.store.append_chat_message(
                role="assistant",
                content=response,
                tool_calls=response_tool_calls,
                message_type="ai",
            )
            emit({"type": "error", "error": response})
            emit({"type": "end"})
            return

        runtime.store.append_chat_message(
            role="assistant",
            content=response,
            tool_calls=response_tool_calls,
            message_type="ai",
        )
        assistant_message = {
            "role": "assistant",
            "content": response,
            "tool_calls": response_tool_calls,
            "message_type": "ai",
        }
        emit(
            {
                "type": "done",
                "assistant_message": assistant_message,
                "tasks": runtime.store.list_tasks(),
            }
        )
        emit({"type": "end"})

    threading.Thread(target=worker, name="chat-stream-worker", daemon=True).start()

    async def event_stream():
        while True:
            event = await event_queue.get()
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event.get("type") == "end":
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat/clear")
def clear_chat() -> Dict[str, Any]:
    runtime.store.clear_chat_messages()
    return {"ok": True}


@app.get("/api/tasks")
def list_tasks() -> List[Dict[str, Any]]:
    return runtime.store.list_tasks()


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: str, req: TaskPatchRequest) -> Dict[str, Any]:
    updated_any = False

    if req.enabled is not None:
        updated = runtime.store.set_task_enabled(task_id, bool(req.enabled))
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found.")
        updated_any = True

    if req.task_prompt is not None:
        updated = runtime.store.update_task_prompt(task_id, req.task_prompt)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found or prompt invalid.")
        updated_any = True

    if not updated_any:
        raise HTTPException(status_code=400, detail="No updates requested.")

    tasks = runtime.store.list_tasks()
    task = next((item for item in tasks if str(item.get("id", "")) == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found after update.")

    return task


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str) -> Dict[str, Any]:
    removed = runtime.store.remove_task(task_id)
    return {"removed": bool(removed)}


@app.post("/api/tasks/{task_id}/run-now")
def run_task_now(task_id: str) -> Dict[str, Any]:
    result = trigger_task_now(
        executor_agent=runtime.executor_agent,
        store=runtime.store,
        scheduler_loop=runtime.scheduler_loop,
        task_id=task_id,
    )
    return result


@app.post("/api/messages/scheduled/{scheduled_event_id}/read")
def mark_scheduled_read(scheduled_event_id: str) -> Dict[str, Any]:
    changed = runtime.store.mark_scheduled_message_read(scheduled_event_id)
    return {"updated": bool(changed)}
