"""Тексты промптов в отдельных файлах (.md / .txt). Загрузка через importlib.resources."""

from __future__ import annotations

from importlib import resources


def load_prompt(name: str) -> str:
    """
    Загрузить файл из пакета `certified_turtles.prompts`.
    `name` — относительный путь, например `protocol_spec.md` или `subagents/coder.md`.
    """
    ref = resources.files(__package__).joinpath(*name.split("/"))
    return ref.read_text(encoding="utf-8")
