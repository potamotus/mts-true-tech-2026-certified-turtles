"""
Конфигурация моделей MWS GPT для автоматического роутинга.

Данные основаны на бенчмарках:
- Качество: MMLU, MMLU-RU, HellaSwag, GSM8K, HumanEval, IFEval
- Скорость: tokens/sec
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskType(Enum):
    """Типы задач для роутинга."""
    CHAT = "chat"
    CODE = "code"
    MATH = "math"
    KNOWLEDGE = "knowledge"
    INSTRUCTION = "instruction"
    VISION = "vision"
    UNKNOWN = "unknown"


class ModelType(Enum):
    """Типы моделей."""
    LLM = "llm"
    VLM = "vlm"
    ASR = "asr"
    EMBEDDING = "embedding"


@dataclass
class ModelSpec:
    """Спецификация модели."""
    id: str
    model_type: ModelType
    speed: float  # tokens/sec
    quality: float  # avg benchmark score (0-100)
    specializations: tuple[TaskType, ...]  # лучше всего подходит для
    fallback: str | None  # резервная модель
    context_window: int = 32000  # размер контекста


# Конфигурация всех моделей
MODELS: dict[str, ModelSpec] = {
    # === LLM (текстовые) ===

    # Быстрые
    "gpt-oss-20b": ModelSpec(
        id="gpt-oss-20b",
        model_type=ModelType.LLM,
        speed=197.3,
        quality=82.86,
        specializations=(TaskType.CHAT,),
        fallback="llama-3.1-8b-instruct",
    ),
    "llama-3.1-8b-instruct": ModelSpec(
        id="llama-3.1-8b-instruct",
        model_type=ModelType.LLM,
        speed=75.4,
        quality=73.49,
        specializations=(TaskType.CHAT,),
        fallback="mws-gpt-alpha",
    ),

    # Сбалансированные
    "gpt-oss-120b": ModelSpec(
        id="gpt-oss-120b",
        model_type=ModelType.LLM,
        speed=149.5,
        quality=88.05,
        specializations=(TaskType.CODE,),  # HumanEval 96.34%
        fallback="gpt-oss-20b",
    ),
    "llama-3.3-70b-instruct": ModelSpec(
        id="llama-3.3-70b-instruct",
        model_type=ModelType.LLM,
        speed=30.1,
        quality=87.57,
        specializations=(TaskType.INSTRUCTION,),  # IFEval 100%
        fallback="gpt-oss-120b",
    ),

    # Высокое качество
    "kimi-k2-instruct": ModelSpec(
        id="kimi-k2-instruct",
        model_type=ModelType.LLM,
        speed=44.8,
        quality=90.85,
        specializations=(TaskType.MATH, TaskType.CODE),  # GSM8K 97%, HumanEval 85%
        fallback="glm-4.6-357b",
    ),
    "glm-4.6-357b": ModelSpec(
        id="glm-4.6-357b",
        model_type=ModelType.LLM,
        speed=35.2,
        quality=90.50,
        specializations=(TaskType.KNOWLEDGE,),  # MMLU 88.58%
        fallback="kimi-k2-instruct",
    ),
    "gemma-3-27b-it": ModelSpec(
        id="gemma-3-27b-it",
        model_type=ModelType.LLM,
        speed=48.2,
        quality=85.23,
        specializations=(TaskType.INSTRUCTION,),  # IFEval 100%
        fallback="llama-3.3-70b-instruct",
    ),

    # Reasoning модели
    "QwQ-32B": ModelSpec(
        id="QwQ-32B",
        model_type=ModelType.LLM,
        speed=35.1,
        quality=88.75,
        specializations=(TaskType.MATH, TaskType.CODE),
        fallback="kimi-k2-instruct",
    ),
    "deepseek-r1-distill-qwen-32b": ModelSpec(
        id="deepseek-r1-distill-qwen-32b",
        model_type=ModelType.LLM,
        speed=57.4,
        quality=85.51,
        specializations=(TaskType.MATH, TaskType.CODE),
        fallback="kimi-k2-instruct",
    ),

    # Прочие LLM
    "qwen2.5-72b-instruct": ModelSpec(
        id="qwen2.5-72b-instruct",
        model_type=ModelType.LLM,
        speed=25.9,
        quality=86.69,
        specializations=(TaskType.CHAT, TaskType.KNOWLEDGE),
        fallback="gpt-oss-120b",
    ),
    "qwen3-coder-480b-a35b": ModelSpec(
        id="qwen3-coder-480b-a35b",
        model_type=ModelType.LLM,
        speed=37.1,
        quality=87.90,
        specializations=(TaskType.CODE,),
        fallback="gpt-oss-120b",
    ),
    "Qwen3-235B-A22B-Instruct-2507-FP8": ModelSpec(
        id="Qwen3-235B-A22B-Instruct-2507-FP8",
        model_type=ModelType.LLM,
        speed=35.7,
        quality=84.40,
        specializations=(TaskType.CHAT,),
        fallback="gpt-oss-120b",
    ),
    "mws-gpt-alpha": ModelSpec(
        id="mws-gpt-alpha",
        model_type=ModelType.LLM,
        speed=81.7,
        quality=70.27,
        specializations=(TaskType.CHAT,),
        fallback="llama-3.1-8b-instruct",
    ),

    # === VLM (vision) ===
    "qwen2.5-vl": ModelSpec(
        id="qwen2.5-vl",
        model_type=ModelType.VLM,
        speed=30.0,  # примерная оценка
        quality=85.0,
        specializations=(TaskType.VISION,),
        fallback="qwen2.5-vl-72b",
    ),
    "qwen2.5-vl-72b": ModelSpec(
        id="qwen2.5-vl-72b",
        model_type=ModelType.VLM,
        speed=20.0,
        quality=90.0,
        specializations=(TaskType.VISION,),
        fallback="cotype-pro-vl-32b",
    ),
    "cotype-pro-vl-32b": ModelSpec(
        id="cotype-pro-vl-32b",
        model_type=ModelType.VLM,
        speed=40.0,
        quality=80.0,
        specializations=(TaskType.VISION,),
        fallback="qwen3-vl-30b-a3b-instruct",
    ),
    "qwen3-vl-30b-a3b-instruct": ModelSpec(
        id="qwen3-vl-30b-a3b-instruct",
        model_type=ModelType.VLM,
        speed=35.0,
        quality=82.0,
        specializations=(TaskType.VISION,),
        fallback=None,
    ),

    # === ASR ===
    "whisper-turbo-local": ModelSpec(
        id="whisper-turbo-local",
        model_type=ModelType.ASR,
        speed=100.0,
        quality=90.0,
        specializations=(),
        fallback="whisper-medium",
    ),
    "whisper-medium": ModelSpec(
        id="whisper-medium",
        model_type=ModelType.ASR,
        speed=50.0,
        quality=85.0,
        specializations=(),
        fallback=None,
    ),
}

# Модели по умолчанию для каждого типа задачи
DEFAULT_MODELS: dict[TaskType, str] = {
    TaskType.CHAT: "gpt-oss-20b",
    TaskType.CODE: "gpt-oss-120b",
    TaskType.MATH: "kimi-k2-instruct",
    TaskType.KNOWLEDGE: "glm-4.6-357b",
    TaskType.INSTRUCTION: "gemma-3-27b-it",
    TaskType.VISION: "qwen2.5-vl",
    TaskType.UNKNOWN: "gpt-oss-120b",
}

# Быстрая модель для классификатора
CLASSIFIER_MODEL = "gpt-oss-20b"


def get_model_spec(model_id: str) -> ModelSpec | None:
    """Получить спецификацию модели по ID."""
    return MODELS.get(model_id)


def get_fallback_chain(model_id: str, max_depth: int = 5) -> list[str]:
    """Получить цепочку fallback моделей."""
    chain = [model_id]
    current = model_id
    for _ in range(max_depth):
        spec = MODELS.get(current)
        if not spec or not spec.fallback:
            break
        chain.append(spec.fallback)
        current = spec.fallback
    return chain


def get_models_by_type(model_type: ModelType) -> list[str]:
    """Получить все модели определённого типа."""
    return [m.id for m in MODELS.values() if m.model_type == model_type]


def get_best_model_for_task(task_type: TaskType) -> str:
    """Получить лучшую модель для типа задачи."""
    return DEFAULT_MODELS.get(task_type, DEFAULT_MODELS[TaskType.UNKNOWN])
