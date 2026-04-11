from __future__ import annotations

import os
from typing import Any

from certified_turtles.agents.loop import run_agent_chat
from certified_turtles.mws_gpt.client import DEFAULT_BASE_URL, MWSGPTClient
from certified_turtles.tools.parent_tools import get_parent_tools


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
        effective_tools = tools if tools is not None else get_parent_tools()
        call_kwargs = dict(extra)
        if effective_tools:
            call_kwargs.setdefault("tools", effective_tools)
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
        return run_agent_chat(
            self._client,
            model,
            messages,
            tools=tools,
            max_tool_rounds=max_tool_rounds,
            **extra,
        )
