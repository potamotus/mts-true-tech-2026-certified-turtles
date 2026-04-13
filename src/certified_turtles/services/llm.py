from __future__ import annotations

import os
from typing import Any

from certified_turtles.agent_debug_log import agent_logger, summarize_messages
from certified_turtles.agents.loop import run_agent_chat
from certified_turtles.mws_gpt.client import DEFAULT_BASE_URL, MWSGPTClient
from certified_turtles.mws_gpt.router import resolve_model
from certified_turtles.services.message_normalize import normalize_chat_messages
from certified_turtles.tools.parent_tools import get_parent_tools

_llm_log = agent_logger("llm")

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
        """Одиночный запрос chat/completions. Если model='auto' — автовыбор модели."""
        messages = normalize_chat_messages(messages)

        # Авто-роутинг если model="auto"
        resolved_model, routing = resolve_model(self._client, model, messages)
        if routing:
            _llm_log.info("Auto-routing: %s", routing.reason)

        _llm_log.debug("chat after normalize tools_explicit=%s\n%s", tools is not None, summarize_messages(messages))
        effective_tools = tools if tools is not None else get_parent_tools()
        call_kwargs = dict(extra)
        if effective_tools:
            call_kwargs.setdefault("tools", effective_tools)
        return self._client.chat_completions(resolved_model, messages, **call_kwargs)

    def chat_plain(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **extra: Any,
    ) -> Any:
        """Один запрос к MWS без тулов. Если model='auto' — автовыбор модели."""
        messages = normalize_chat_messages(messages)

        # Авто-роутинг если model="auto"
        resolved_model, routing = resolve_model(self._client, model, messages)
        if routing:
            _llm_log.info("Auto-routing (plain): %s", routing.reason)

        _llm_log.debug("chat_plain after normalize\n%s", summarize_messages(messages))
        call_kwargs = {k: v for k, v in extra.items() if k not in ("tools", "tool_choice")}
        return self._client.chat_completions(resolved_model, messages, **call_kwargs)

    def run_agent(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tool_rounds: int = 10,
        tools: list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Полный agent-цикл с тулами. Если model='auto' — автовыбор модели."""
        messages = normalize_chat_messages(messages)

        # Авто-роутинг если model="auto"
        resolved_model, routing = resolve_model(self._client, model, messages)
        if routing:
            _llm_log.info("Auto-routing (agent): %s", routing.reason)

        rounds = clamp_agent_tool_rounds(max_tool_rounds)
        _llm_log.debug(
            "run_agent after normalize max_tool_rounds=%s tools_explicit=%s\n%s",
            rounds,
            tools is not None,
            summarize_messages(messages),
        )
        return run_agent_chat(
            self._client,
            resolved_model,
            messages,
            tools=tools,
            max_tool_rounds=rounds,
            **extra,
        )
