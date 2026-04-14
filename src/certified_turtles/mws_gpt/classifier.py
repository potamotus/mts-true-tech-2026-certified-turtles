"""
LLM-классификатор задач для автоматического роутинга.

Использует быструю модель для определения типа задачи.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from certified_turtles.mws_gpt.model_config import CLASSIFIER_MODEL, TaskType

if TYPE_CHECKING:
    from certified_turtles.mws_gpt.client import MWSGPTClient


CLASSIFIER_PROMPT = """Ты классификатор задач. Определи тип задачи пользователя.

Типы задач:
- code: написание кода, отладка, программирование
- math: математика, вычисления, уравнения, статистика
- knowledge: вопросы о фактах, объяснения, "что такое", история, наука
- instruction: пошаговые инструкции, строгое следование формату
- chat: обычный разговор, приветствия, личные вопросы

Ответь ТОЛЬКО одним словом: code, math, knowledge, instruction или chat.

Задача пользователя:
{user_message}

Тип задачи:"""


_TASK_TYPE_MAP = {
    "code": TaskType.CODE,
    "math": TaskType.MATH,
    "knowledge": TaskType.KNOWLEDGE,
    "instruction": TaskType.INSTRUCTION,
    "chat": TaskType.CHAT,
}


class TaskClassifier:
    """Классификатор задач на основе LLM."""

    def __init__(self, client: "MWSGPTClient"):
        self._client = client
        self._cache: dict[str, tuple[TaskType, float]] = {}
        self._cache_ttl = 300  # 5 минут

    def _get_cache_key(self, text: str) -> str:
        """Ключ кэша - первые 200 символов."""
        return text[:200].strip().lower()

    def _extract_user_message(self, messages: list[dict[str, Any]]) -> str:
        """Извлекает последнее сообщение пользователя."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content[:1000]
                if isinstance(content, list):
                    texts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            texts.append(part.get("text", ""))
                    return " ".join(texts)[:1000]
        return ""

    def classify(self, messages: list[dict[str, Any]]) -> TaskType:
        """
        Классифицирует задачу по сообщениям.

        Использует LLM для определения типа задачи.
        Результат кэшируется на 5 минут.
        """
        user_message = self._extract_user_message(messages)
        if not user_message:
            return TaskType.CHAT

        # Проверяем кэш
        cache_key = self._get_cache_key(user_message)
        if cache_key in self._cache:
            task_type, cached_at = self._cache[cache_key]
            if time.time() - cached_at < self._cache_ttl:
                return task_type

        # Вызываем LLM-классификатор
        try:
            task_type = self._classify_with_llm(user_message)
        except Exception:
            # При ошибке используем эвристики
            task_type = self._classify_with_heuristics(user_message)

        # Сохраняем в кэш
        self._cache[cache_key] = (task_type, time.time())
        return task_type

    def _classify_with_llm(self, user_message: str) -> TaskType:
        """Классификация через LLM."""
        prompt = CLASSIFIER_PROMPT.format(user_message=user_message)

        response = self._client.chat_completions(
            model=CLASSIFIER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.0,
        )

        if not response or "choices" not in response:
            return TaskType.UNKNOWN

        content = response["choices"][0].get("message", {}).get("content", "").strip().lower()

        # Ищем тип задачи в ответе
        for key, task_type in _TASK_TYPE_MAP.items():
            if key in content:
                return task_type

        return TaskType.UNKNOWN

    def _classify_with_heuristics(self, text: str) -> TaskType:
        """Fallback: классификация по ключевым словам."""
        text_lower = text.lower()

        # Код
        code_keywords = [
            "код", "code", "функци", "function", "класс", "class",
            "python", "javascript", "typescript", "java", "c++",
            "напиши", "write", "реализуй", "implement", "debug",
            "```", "def ", "import ", "const ", "let ", "var "
        ]
        if any(kw in text_lower for kw in code_keywords):
            return TaskType.CODE

        # Математика
        math_keywords = [
            "вычисли", "calculate", "посчитай", "compute",
            "реши", "solve", "уравнени", "equation",
            "формул", "formula", "интеграл", "производн",
            "математик", "math", "алгебр", "геометр",
            "+", "-", "*", "/", "=", "^", "√"
        ]
        if any(kw in text_lower for kw in math_keywords):
            return TaskType.MATH

        # Знания
        knowledge_keywords = [
            "что такое", "what is", "кто такой", "who is",
            "объясни", "explain", "расскажи", "tell me",
            "почему", "why", "как работает", "how does",
            "истори", "history", "факт", "fact"
        ]
        if any(kw in text_lower for kw in knowledge_keywords):
            return TaskType.KNOWLEDGE

        # Инструкции
        instruction_keywords = [
            "пошагово", "step by step", "инструкци", "instruction",
            "сделай именно", "do exactly", "строго", "strictly",
            "по порядку", "in order", "список", "list"
        ]
        if any(kw in text_lower for kw in instruction_keywords):
            return TaskType.INSTRUCTION

        # По умолчанию - чат
        return TaskType.CHAT


# Глобальный экземпляр классификатора (инициализируется при первом использовании)
_classifier: TaskClassifier | None = None


def get_classifier(client: "MWSGPTClient") -> TaskClassifier:
    """Получить или создать классификатор."""
    global _classifier
    if _classifier is None:
        _classifier = TaskClassifier(client)
    return _classifier


def classify_task(client: "MWSGPTClient", messages: list[dict[str, Any]]) -> TaskType:
    """Удобная функция для классификации задачи."""
    classifier = get_classifier(client)
    return classifier.classify(messages)
