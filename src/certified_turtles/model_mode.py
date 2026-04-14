"""
Виртуальные id моделей: `deep_research::<id MWS>` → ct_mode + базовая модель (опционально, см. CT_LIST_MODE_VARIANTS).

Рекомендуемый UX без раздувания списка: отдельное подключение Open WebUI с base
`.../v1/m/deep_research` (см. GET/POST под префиксом /v1/m/{mode}/).
"""

from __future__ import annotations

import os
from typing import Any

from certified_turtles.chat_modes import canonical_mode_ids


def split_virtual_model(model: str) -> tuple[str, str | None]:
    """
    Возвращает (реальный_id_модели, режим_или_None).
    Если строка не вида `режим::остальное` или режим неизвестен — (model, None).
    """
    if "::" not in model:
        return model, None
    left, right = model.split("::", 1)
    if not right.strip():
        return model, None
    allowed = canonical_mode_ids()
    if left not in allowed:
        return model, None
    return right, left


def apply_virtual_model_to_body(body: dict[str, Any]) -> None:
    """
    Мутирует body: подставляет реальный model и при необходимости ct_mode.
    Явный ct_mode в JSON имеет приоритет — тогда только снимает префикс с model.
    """
    m = body.get("model")
    if not isinstance(m, str) or not m.strip():
        return
    explicit_ct = isinstance(body.get("ct_mode"), str) and body["ct_mode"].strip()
    if explicit_ct:
        real, _ = split_virtual_model(m)
        body["model"] = real
        return
    real, mode = split_virtual_model(m)
    if mode:
        body["model"] = real
        body["ct_mode"] = mode


def should_merge_virtual_models_into_list() -> bool:
    """Если true — в GET /v1/models дублируются модели как режим::id (много записей). По умолчанию выключено."""
    v = (os.environ.get("CT_LIST_MODE_VARIANTS") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def merge_virtual_models_openai_payload(payload: Any) -> Any:
    """
    Дополняет ответ GET /v1/models: для каждой базовой модели добавляет варианты `режим::<id>`.
    Уже виртуальные id (с `::`) не дублируются.
    """
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if not isinstance(data, list):
        return payload
    modes = sorted(canonical_mode_ids())
    extra: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or "::" in mid:
            continue
        for mode in modes:
            row = dict(item)
            row["id"] = f"{mode}::{mid}"
            row["owned_by"] = "certified-turtles+mode"
            extra.append(row)
    out = dict(payload)
    out["data"] = list(data) + extra
    return out
