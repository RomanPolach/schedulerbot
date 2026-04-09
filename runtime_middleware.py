from __future__ import annotations

import json
import threading
import traceback
from contextvars import ContextVar
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict, List
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware
from langchain.agents.middleware.types import ToolCallRequest

from runtime_config import (
    AGENT_TRACE_ENABLED,
    AGENT_TRACE_FILE,
    AGENT_TRACE_FORMAT,
    AGENT_TRACE_MAX_TEXT_CHARS,
    GEMINI_INCLUDE_THOUGHTS,
    GEMINI_THINKING_BUDGET,
    GEMINI_THINKING_LEVEL,
    MAX_TOOL_CALLS_PER_RUN,
)


_HUMAN_AI_MESSAGE_TYPES = {"HumanMessage", "UserMessage", "AIMessage", "AssistantMessage"}
_HUMAN_MESSAGE_TYPES = {"HumanMessage", "UserMessage"}
MAX_HUMAN_AI_MESSAGES = 6


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= 300:
                out["__truncated_items__"] = len(value) - idx
                break
            out[str(key)] = _json_safe(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out_items: List[Any] = []
        for idx, item in enumerate(items):
            if idx >= 300:
                out_items.append(f"... truncated {len(items) - idx} items")
                break
            out_items.append(_json_safe(item, depth + 1))
        return out_items
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(), depth + 1)
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return _json_safe(value.dict(), depth + 1)
        except Exception:
            pass
    return str(value)


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def _truncate_value(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return _truncate_text(value, max_chars)
    if isinstance(value, dict):
        return {key: _truncate_value(item, max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_value(item, max_chars) for item in value]
    return value


def _serialize_message(message: Any, max_text_chars: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type(message).__name__}
    if message is None:
        return out

    raw_content = getattr(message, "content", "")
    if isinstance(raw_content, list):
        text_parts: List[str] = []
        for item in raw_content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    text_parts.append(str(text))
        raw_content = "\n".join(text_parts) if text_parts else raw_content
    elif isinstance(raw_content, dict):
        text = raw_content.get("text")
        if text:
            raw_content = str(text)

    out["content"] = _truncate_value(_json_safe(raw_content), max_text_chars)

    tool_calls = getattr(message, "tool_calls", None)
    if isinstance(tool_calls, list) and tool_calls:
        out["tool_calls"] = _truncate_value(_json_safe(tool_calls), max_text_chars)

    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and additional_kwargs:
        out["additional_kwargs"] = _truncate_value(_json_safe(additional_kwargs), max_text_chars)
        signatures = additional_kwargs.get("__gemini_function_call_thought_signatures__")
        if isinstance(signatures, dict) and signatures:
            out["reasoning_signature_count"] = len(signatures)

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict) and response_metadata:
        out["response_metadata"] = _truncate_value(_json_safe(response_metadata), max_text_chars)

    for attr_name in ("id", "name", "tool_call_id", "status"):
        value = getattr(message, attr_name, None)
        if value is not None and value != "":
            out[attr_name] = str(value)

    # Extract provider-exposed reasoning/thinking blocks when available.
    reasoning_text = _extract_reasoning_text(message, max_text_chars)
    if reasoning_text:
        out["reasoning"] = reasoning_text

    msg_type = type(message).__name__
    if msg_type in {"AIMessage", "AssistantMessage"}:
        tool_calls = getattr(message, "tool_calls", None)
        if isinstance(tool_calls, list) and tool_calls:
            names: List[str] = []
            for call in tool_calls:
                if isinstance(call, dict):
                    name = call.get("name")
                    if name:
                        names.append(str(name))
            out["decision"] = (
                f"tool_call: {', '.join(names)}"
                if names
                else "tool_call"
            )
        elif str(out.get("content", "")).strip():
            out["decision"] = "final_answer"
    return out


def _extract_reasoning_text(message: Any, max_text_chars: int) -> str:
    pieces: List[str] = []

    def _add_piece(value: Any) -> None:
        text = _collapse_text(value)
        if text and text not in {"[]", "{}"}:
            pieces.append(text)

    # Primary path: LangChain-standardized content blocks.
    # Example for Gemini with include_thoughts=True:
    #   [{"type":"reasoning","reasoning":"..."}, {"type":"text","text":"..."}]
    content_blocks = getattr(message, "content_blocks", None)
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")).lower() == "reasoning":
                _add_piece(
                    block.get("reasoning")
                    or block.get("text")
                    or block.get("summary")
                    or block.get("content")
                )

    # Fallbacks for provider-specific message payloads.
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", "")).lower()
            if block_type in {"reasoning", "thinking", "analysis", "reasoning_summary"}:
                _add_piece(
                    block.get("thinking")
                    or block.get("text")
                    or block.get("summary")
                    or block.get("content")
                )

    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        for key in ("reasoning", "reasoning_content", "thinking", "analysis", "reasoning_summary"):
            if key in additional_kwargs:
                _add_piece(additional_kwargs.get(key))

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        for key in ("reasoning", "reasoning_content", "thinking", "analysis", "reasoning_summary"):
            if key in response_metadata:
                _add_piece(response_metadata.get(key))

    if not pieces:
        return ""
    merged = " | ".join(pieces)
    return _truncate_text(merged, max_text_chars)


def _serialize_model_name(model: Any) -> str:
    for attr_name in ("model", "model_name", "name"):
        value = getattr(model, attr_name, None)
        if value:
            return str(value)
    return type(model).__name__


def _serialize_tools(tools: List[Any]) -> List[str]:
    names: List[str] = []
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
            names.append(str(name) if name else "dict_tool")
            continue
        names.append(str(getattr(tool, "name", type(tool).__name__)))
    return names


_CURRENT_AGENT_RUN_ID: ContextVar[str] = ContextVar("current_agent_run_id", default="")
_TRACE_LOCAL = threading.local()


def _collapse_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    try:
        return " ".join(json.dumps(value, ensure_ascii=False).split())
    except Exception:
        return " ".join(str(value).split())


def _message_preview(message: Dict[str, Any], max_chars: int) -> str:
    msg_type = str(message.get("type", "Message"))
    content = _collapse_text(message.get("content", ""))
    if len(content) > max_chars:
        content = f"{content[:max_chars]}... [truncated]"
    preview = f"{msg_type}: {content}" if content else msg_type
    decision = str(message.get("decision", "")).strip()
    if decision:
        preview += f" | decision={decision}"
    reasoning = str(message.get("reasoning", "")).strip()
    if reasoning:
        clipped = reasoning if len(reasoning) <= max_chars else f"{reasoning[:max_chars]}... [truncated]"
        preview += f" | reasoning={clipped}"
    sig_count = message.get("reasoning_signature_count")
    if isinstance(sig_count, int) and sig_count > 0:
        preview += f" | reasoning_signatures={sig_count}"
    return preview


def _format_pretty_event(event: Dict[str, Any], max_chars: int) -> str:
    preview_chars = min(max_chars, 350)
    ts = str(event.get("ts_utc", ""))
    agent = str(event.get("agent", "agent"))
    name = str(event.get("event", "event")).upper()
    run_id = str(event.get("run_id", ""))
    call_id = str(event.get("call_id", ""))
    header = f"[{ts}] [{agent}] {name}"
    if run_id:
        header += f" run={run_id}"
    if call_id:
        header += f" call={call_id}"

    lines = [header]
    duration = event.get("duration_ms")
    if isinstance(duration, int):
        lines.append(f"duration_ms: {duration}")

    if "model" in event:
        lines.append(f"model: {event.get('model')}")
    model_settings = event.get("model_settings")
    if isinstance(model_settings, dict) and model_settings:
        settings_preview = json.dumps(model_settings, ensure_ascii=False)
        if len(settings_preview) > preview_chars:
            settings_preview = settings_preview[:preview_chars] + "... [truncated]"
        lines.append(f"model_settings: {settings_preview}")
    if "tool_name" in event:
        lines.append(f"tool: {event.get('tool_name')}")
    if "message_count" in event:
        lines.append(f"message_count: {event.get('message_count')}")
    if "latest_user" in event and event.get("latest_user"):
        lines.append(f"latest_user: {_collapse_text(event.get('latest_user'))[:preview_chars]}")
    if "tools" in event and isinstance(event.get("tools"), list):
        lines.append(f"available_tools: {', '.join(str(t) for t in event.get('tools', []))}")

    messages = event.get("messages")
    latest_ai: Dict[str, Any] | None = None
    if isinstance(messages, list) and messages:
        lines.append("messages:")
        preview_count = min(4, len(messages))
        for idx, message in enumerate(messages[-preview_count:], 1):
            if isinstance(message, dict):
                lines.append(f"  {idx}. {_message_preview(message, preview_chars)}")
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if str(message.get("type", "")).lower() in {"aimessage", "assistantmessage"}:
                latest_ai = message
                break

    if latest_ai is not None:
        decision = str(latest_ai.get("decision", "")).strip()
        reasoning = str(latest_ai.get("reasoning", "")).strip()
        if decision:
            lines.append(f"decision: {decision}")
        if reasoning:
            lines.append(f"reasoning: {reasoning[:preview_chars]}")
        elif decision:
            lines.append("reasoning: (not exposed by model/provider)")

    if "tool_call" in event:
        lines.append("tool_call:")
        lines.append(json.dumps(event.get("tool_call"), ensure_ascii=False, indent=2)[:max_chars * 2])
    if "result" in event:
        result_text = _collapse_text(event.get("result"))
        lines.append(f"result: {result_text[:preview_chars]}")
    if "structured_response" in event and event.get("structured_response") is not None:
        structured = _collapse_text(event.get("structured_response"))
        lines.append(f"structured_response: {structured[:preview_chars]}")
    if "error" in event:
        lines.append(f"error: {event.get('error')}")
    if "traceback" in event:
        tb = str(event.get("traceback", ""))
        lines.append("traceback:")
        lines.append(tb[: max_chars * 3])

    return "\n".join(lines) + "\n" + ("-" * 100) + "\n"


class TraceLogger:
    def __init__(self, *, enabled: bool, path: str, max_text_chars: int, fmt: str) -> None:
        self.enabled = enabled
        self.path = path
        self.max_text_chars = max_text_chars
        self.fmt = fmt
        self._lock = threading.Lock()
        self._dir_ready = False

    def log(self, event: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            payload = _truncate_value(_json_safe(event), self.max_text_chars)
            if self.fmt == "jsonl":
                rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            else:
                rendered = _format_pretty_event(payload, self.max_text_chars)
            with self._lock:
                if not self._dir_ready:
                    from pathlib import Path

                    Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                    self._dir_ready = True
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(rendered)
        except Exception:
            # Never break agent execution because trace logging failed.
            return


class AgentTraceMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, logger: TraceLogger, agent_name: str) -> None:
        self._logger = logger
        self._agent_name = agent_name

    def _force_gemini_thinking_settings(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        settings = dict(request.model_settings or {})
        model_name = _serialize_model_name(request.model).lower()
        if "gemini" not in model_name:
            return request

        if GEMINI_INCLUDE_THOUGHTS:
            settings["include_thoughts"] = True
        settings["thinking_level"] = GEMINI_THINKING_LEVEL
        if GEMINI_THINKING_BUDGET is not None:
            settings["thinking_budget"] = GEMINI_THINKING_BUDGET

        if settings == dict(request.model_settings or {}):
            return request
        return request.override(model_settings=settings)

    def _state_messages(self, state: Any) -> List[Any]:
        if isinstance(state, dict):
            messages = state.get("messages", [])
            return messages if isinstance(messages, list) else []
        messages = getattr(state, "messages", None)
        return messages if isinstance(messages, list) else []

    def _latest_user_text(self, messages: List[Any]) -> str:
        for message in reversed(messages):
            msg_type = type(message).__name__
            if msg_type in _HUMAN_MESSAGE_TYPES:
                return _collapse_text(getattr(message, "content", ""))
        return ""

    def _base(self, event: str, call_id: str) -> Dict[str, Any]:
        run_id = getattr(_TRACE_LOCAL, "run_id", "") or _CURRENT_AGENT_RUN_ID.get("")
        return {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "agent": self._agent_name,
            "event": event,
            "call_id": call_id,
            "run_id": run_id,
        }

    def before_agent(self, state: Any, runtime: Any) -> Dict[str, Any] | None:
        run_id = uuid4().hex[:12]
        _CURRENT_AGENT_RUN_ID.set(run_id)
        _TRACE_LOCAL.run_id = run_id
        messages = self._state_messages(state)
        self._logger.log(
            {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "agent": self._agent_name,
                "event": "run_start",
                "run_id": run_id,
                "message_count": len(messages),
                "latest_user": self._latest_user_text(messages),
            }
        )
        return None

    async def abefore_agent(self, state: Any, runtime: Any) -> Dict[str, Any] | None:
        return self.before_agent(state, runtime)

    def after_agent(self, state: Any, runtime: Any) -> Dict[str, Any] | None:
        messages = self._state_messages(state)
        self._logger.log(
            {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "agent": self._agent_name,
                "event": "run_end",
                "run_id": getattr(_TRACE_LOCAL, "run_id", "") or _CURRENT_AGENT_RUN_ID.get(""),
                "message_count": len(messages),
            }
        )
        _TRACE_LOCAL.run_id = ""
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> Dict[str, Any] | None:
        return self.after_agent(state, runtime)

    def _log_model_start(self, call_id: str, request: ModelRequest[Any]) -> None:
        self._logger.log(
            {
                **self._base("model_start", call_id),
                "model": _serialize_model_name(request.model),
                "tool_choice": _json_safe(request.tool_choice),
                "tools": _serialize_tools(request.tools),
                "model_settings": _json_safe(request.model_settings),
                "system_message": _serialize_message(request.system_message, self._logger.max_text_chars)
                if request.system_message
                else None,
                "messages": [_serialize_message(msg, self._logger.max_text_chars) for msg in request.messages],
            }
        )

    def _log_model_end(self, call_id: str, started_at: float, response: ModelResponse[Any]) -> None:
        self._logger.log(
            {
                **self._base("model_end", call_id),
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "messages": [_serialize_message(msg, self._logger.max_text_chars) for msg in response.result],
                "structured_response": _truncate_value(
                    _json_safe(response.structured_response), self._logger.max_text_chars
                ),
            }
        )

    def _log_model_error(self, call_id: str, started_at: float, error: Exception) -> None:
        self._logger.log(
            {
                **self._base("model_error", call_id),
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": repr(error),
                "traceback": traceback.format_exc(limit=8),
            }
        )

    def _log_tool_start(self, call_id: str, request: ToolCallRequest) -> None:
        tool_call = _json_safe(request.tool_call)
        self._logger.log(
            {
                **self._base("tool_start", call_id),
                "tool_name": str((request.tool_call or {}).get("name") or getattr(request.tool, "name", "unknown")),
                "tool_call": _truncate_value(tool_call, self._logger.max_text_chars),
            }
        )

    def _log_tool_end(self, call_id: str, started_at: float, result: Any) -> None:
        payload = result
        if hasattr(result, "content") or hasattr(result, "tool_call_id"):
            payload = _serialize_message(result, self._logger.max_text_chars)
        else:
            payload = _truncate_value(_json_safe(result), self._logger.max_text_chars)
        self._logger.log(
            {
                **self._base("tool_end", call_id),
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "result": payload,
            }
        )

    def _log_tool_error(self, call_id: str, started_at: float, error: Exception) -> None:
        self._logger.log(
            {
                **self._base("tool_error", call_id),
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": repr(error),
                "traceback": traceback.format_exc(limit=8),
            }
        )

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        request = self._force_gemini_thinking_settings(request)
        call_id = uuid4().hex
        started_at = perf_counter()
        self._log_model_start(call_id, request)
        try:
            response = handler(request)
            self._log_model_end(call_id, started_at, response)
            return response
        except Exception as exc:
            self._log_model_error(call_id, started_at, exc)
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        request = self._force_gemini_thinking_settings(request)
        call_id = uuid4().hex
        started_at = perf_counter()
        self._log_model_start(call_id, request)
        try:
            response = await handler(request)
            self._log_model_end(call_id, started_at, response)
            return response
        except Exception as exc:
            self._log_model_error(call_id, started_at, exc)
            raise

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        call_id = uuid4().hex
        started_at = perf_counter()
        self._log_tool_start(call_id, request)
        try:
            result = handler(request)
            self._log_tool_end(call_id, started_at, result)
            return result
        except Exception as exc:
            self._log_tool_error(call_id, started_at, exc)
            raise

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        call_id = uuid4().hex
        started_at = perf_counter()
        self._log_tool_start(call_id, request)
        try:
            result = await handler(request)
            self._log_tool_end(call_id, started_at, result)
            return result
        except Exception as exc:
            self._log_tool_error(call_id, started_at, exc)
            raise


def _trim_to_last_human_ai_messages(messages: List[Any], max_messages: int) -> List[Any]:
    if max_messages <= 0:
        return []

    conversational_indexes = [
        idx for idx, message in enumerate(messages) if type(message).__name__ in _HUMAN_AI_MESSAGE_TYPES
    ]
    if len(conversational_indexes) <= max_messages:
        return list(messages)

    cutoff_index = conversational_indexes[-max_messages]

    # Keep provider-compatible turn boundaries: start from a user message.
    # This avoids cutting into a function/tool-call chain mid-turn.
    start_index = cutoff_index
    while start_index >= 0 and type(messages[start_index]).__name__ not in _HUMAN_MESSAGE_TYPES:
        start_index -= 1

    if start_index < 0:
        # Fallback: don't trim if we cannot find a safe boundary.
        return list(messages)

    return list(messages[start_index:])


class LimitHumanAIHistoryMiddleware(AgentMiddleware[Any, Any, Any]):
    def _apply_model_settings_overrides(self, request: ModelRequest[Any]) -> dict[str, Any]:
        settings = dict(request.model_settings or {})
        model_name = _serialize_model_name(request.model).lower()

        # Use LangChain's official per-call model settings pathway.
        # This ensures Gemini thinking flags are sent on agent calls
        # even after internal tool-binding wrappers.
        if "gemini" in model_name:
            if GEMINI_INCLUDE_THOUGHTS:
                settings["include_thoughts"] = True
            settings["thinking_level"] = GEMINI_THINKING_LEVEL
            if GEMINI_THINKING_BUDGET is not None:
                settings["thinking_budget"] = GEMINI_THINKING_BUDGET
        return settings

    def _trim_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        trimmed_messages = _trim_to_last_human_ai_messages(request.messages, MAX_HUMAN_AI_MESSAGES)
        settings = self._apply_model_settings_overrides(request)

        same_messages = len(trimmed_messages) == len(request.messages)
        same_settings = settings == dict(request.model_settings or {})
        if same_messages and same_settings:
            return request
        return request.override(messages=trimmed_messages, model_settings=settings)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._trim_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._trim_request(request))


_trace_logger = TraceLogger(
    enabled=AGENT_TRACE_ENABLED,
    path=str(AGENT_TRACE_FILE),
    max_text_chars=AGENT_TRACE_MAX_TEXT_CHARS,
    fmt=AGENT_TRACE_FORMAT,
)


def create_tool_call_limit_middleware() -> ToolCallLimitMiddleware:
    return ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS_PER_RUN, exit_behavior="continue")


def create_agent_trace_middleware(agent_name: str) -> AgentTraceMiddleware:
    return AgentTraceMiddleware(logger=_trace_logger, agent_name=agent_name)


limit_human_ai_history_middleware = LimitHumanAIHistoryMiddleware()
