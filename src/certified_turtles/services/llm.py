from __future__ import annotations

import os
from typing import Any

from certified_turtles.agents.loop import run_agent_chat
from certified_turtles.mws_gpt.client import DEFAULT_BASE_URL, MWSGPTClient
from certified_turtles.services.message_normalize import normalize_chat_messages
from certified_turtles.tools.parent_tools import get_parent_tools

# Согласовано с `AgentChatRequest.max_tool_rounds` (API) и телом Open WebUI к `/v1/chat/completions`.
_MAX_AGENT_TOOL_ROUNDS = 40
_MIN_AGENT_TOOL_ROUNDS = 1


def clamp_agent_tool_rounds(value: Any) -> int:
    """Ограничивает число раундов tool-calling (защита от зависаний и мусора в JSON)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 10
    return max(_MIN_AGENT_TOOL_ROUNDS, min(_MAX_AGENT_TOOL_ROUNDS, n))


class LLMService:
    """Единая точка входа в LLM: list_models, обычный chat и agent-цикл с тулами.

    Все исходящие обращения к MWS GPT идут через этот фасад, чтобы тулы,
    параметры и клиент жили в одном месте (а не плодились в api/cli).
    """

    def __init__(self, client: MWSGPTClient):
        self._client = client

    @classmethod
    def from_env(cls) -> "LLMService":
        """Собирает сервис из переменных окружения (`MWS_API_KEY`, `MWS_API_BASE`)."""
        client = MWSGPTClient(base_url=os.environ.get("MWS_API_BASE", DEFAULT_BASE_URL))
        return cls(client)

    @property
    def client(self) -> MWSGPTClient:
        return self._client

    def list_models(self) -> Any:
        return self._client.list_models()

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> Any:
        """Одиночный запрос chat/completions. Если `tools` не заданы — подставляем полный каталог родителя."""
        messages = normalize_chat_messages(messages)
        effective_tools = tools if tools is not None else get_parent_tools()
        call_kwargs = dict(extra)
        if effective_tools:
            call_kwargs.setdefault("tools", effective_tools)
        return self._client.chat_completions(model, messages, **call_kwargs)

    def chat_plain(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **extra: Any,
    ) -> Any:
        """Один запрос к MWS без тулов и без агентского JSON-цикла (как обычный чат в Open WebUI)."""
        messages = normalize_chat_messages(messages)
        call_kwargs = {k: v for k, v in extra.items() if k not in ("tools", "tool_choice")}
        return self._client.chat_completions(model, messages, **call_kwargs)

    def run_agent(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tool_rounds: int = 10,
        tools: list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Полный agent-цикл с тулами (примитивы + под-агенты). Возвращает `messages`, `completion`, метаданные."""
        messages = normalize_chat_messages(messages)
        rounds = clamp_agent_tool_rounds(max_tool_rounds)
        return run_agent_chat(
            self._client,
            model,
            messages,
            tools=tools,
            max_tool_rounds=rounds,
            **extra,
        )
