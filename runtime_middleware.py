from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, List

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse


_HUMAN_AI_MESSAGE_TYPES = {"HumanMessage", "UserMessage", "AIMessage", "AssistantMessage"}
MAX_HUMAN_AI_MESSAGES = 10


def _trim_to_last_human_ai_messages(messages: List[Any], max_messages: int) -> List[Any]:
    if max_messages <= 0:
        return []

    conversational_indexes = [
        idx for idx, message in enumerate(messages) if type(message).__name__ in _HUMAN_AI_MESSAGE_TYPES
    ]
    if len(conversational_indexes) <= max_messages:
        return list(messages)

    cutoff_index = conversational_indexes[-max_messages]
    return list(messages[cutoff_index:])


class LimitHumanAIHistoryMiddleware(AgentMiddleware[Any, Any, Any]):
    def _trim_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        trimmed_messages = _trim_to_last_human_ai_messages(request.messages, MAX_HUMAN_AI_MESSAGES)
        if len(trimmed_messages) == len(request.messages):
            return request
        return request.override(messages=trimmed_messages)

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


limit_human_ai_history_middleware = LimitHumanAIHistoryMiddleware()
