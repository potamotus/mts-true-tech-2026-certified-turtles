"""
Мультимодальный роутер для автоматического выбора модели.

Определяет тип входных данных и задачи, выбирает оптимальную модель.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from certified_turtles.agent_debug_log import agent_logger
from certified_turtles.mws_gpt.classifier import classify_task
from certified_turtles.mws_gpt.model_config import (
    DEFAULT_MODELS,
    MODELS,
    ModelType,
    TaskType,
    get_best_model_for_task,
    get_fallback_chain,
)

if TYPE_CHECKING:
    from certified_turtles.mws_gpt.client import MWSGPTClient

_router_log = agent_logger("router")


@dataclass
class RoutingResult:
    """Результат роутинга."""
    model: str
    task_type: TaskType
    input_type: str  # "text", "vision", "audio"
    reason: str
    fallback_used: bool = False


class ModelRouter:
    """Мультимодальный роутер для выбора модели."""

    def __init__(self, client: "MWSGPTClient"):
        self._client = client
        self._available_models: set[str] | None = None
        self._models_cache_time: float = 0
        self._cache_ttl = 300  # 5 минут

    def _refresh_available_models(self) -> set[str]:
        """Обновляет кэш доступных моделей."""
        now = time.time()
        if self._available_models is not None and now - self._models_cache_time < self._cache_ttl:
            return self._available_models

        try:
            response = self._client.list_models()
            models = {m["id"] for m in response.get("data", [])}
            self._available_models = models
            self._models_cache_time = now
            _router_log.debug("Refreshed available models: %d models", len(models))
        except Exception as e:
            _router_log.warning("Failed to refresh models list: %s", e)
            if self._available_models is None:
                self._available_models = set(MODELS.keys())

        return self._available_models

    def _has_images(self, messages: list[dict[str, Any]]) -> bool:
        """Проверяет наличие изображений в сообщениях."""
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "image_url":
                            return True
                        if part.get("type") == "input_image":
                            return True
        return False

    def _detect_input_type(self, messages: list[dict[str, Any]]) -> str:
        """Определяет тип входных данных."""
        if self._has_images(messages):
            return "vision"
        return "text"

    def _select_model_with_fallback(
        self,
        preferred_model: str,
        available: set[str],
    ) -> tuple[str, bool]:
        """Выбирает модель с учётом fallback."""
        chain = get_fallback_chain(preferred_model)

        for model in chain:
            if model in available:
                fallback_used = model != preferred_model
                return model, fallback_used

        # Если ничего не доступно - возвращаем preferred и надеемся
        _router_log.warning(
            "No models from fallback chain available: %s. Using preferred: %s",
            chain, preferred_model
        )
        return preferred_model, False

    def route(self, messages: list[dict[str, Any]]) -> RoutingResult:
        """
        Выбирает оптимальную модель для запроса.

        1. Определяет тип входных данных (текст/изображение)
        2. Если изображение - выбирает VLM
        3. Иначе классифицирует задачу через LLM
        4. Выбирает модель по типу задачи
        5. Применяет fallback если модель недоступна
        """
        available = self._refresh_available_models()
        input_type = self._detect_input_type(messages)

        # Vision - используем VLM
        if input_type == "vision":
            preferred = get_best_model_for_task(TaskType.VISION)
            model, fallback_used = self._select_model_with_fallback(preferred, available)
            reason = f"Image detected → VLM: {model}"
            if fallback_used:
                reason += f" (fallback from {preferred})"

            _router_log.info("Routing: %s", reason)
            return RoutingResult(
                model=model,
                task_type=TaskType.VISION,
                input_type=input_type,
                reason=reason,
                fallback_used=fallback_used,
            )

        # Текст - классифицируем задачу
        task_type = classify_task(self._client, messages)
        preferred = get_best_model_for_task(task_type)
        model, fallback_used = self._select_model_with_fallback(preferred, available)

        reason = f"Task: {task_type.value} → {model}"
        if fallback_used:
            reason += f" (fallback from {preferred})"

        _router_log.info("Routing: %s", reason)
        return RoutingResult(
            model=model,
            task_type=task_type,
            input_type=input_type,
            reason=reason,
            fallback_used=fallback_used,
        )


# Глобальный роутер
_router: ModelRouter | None = None


def get_router(client: "MWSGPTClient") -> ModelRouter:
    """Получить или создать роутер."""
    global _router
    if _router is None:
        _router = ModelRouter(client)
    return _router


def auto_select_model(
    client: "MWSGPTClient",
    messages: list[dict[str, Any]],
) -> RoutingResult:
    """
    Автоматически выбирает модель для запроса.

    Args:
        client: MWS GPT клиент
        messages: Сообщения в формате OpenAI

    Returns:
        RoutingResult с выбранной моделью и причиной выбора
    """
    router = get_router(client)
    return router.route(messages)


def resolve_model(
    client: "MWSGPTClient",
    model: str,
    messages: list[dict[str, Any]],
) -> tuple[str, RoutingResult | None]:
    """
    Разрешает имя модели.

    Если model="auto" - выбирает автоматически.
    Иначе возвращает модель как есть.

    Returns:
        (resolved_model, routing_result или None)
    """
    if model.lower() == "auto":
        result = auto_select_model(client, messages)
        return result.model, result

    return model, None
