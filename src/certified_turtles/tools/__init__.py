from __future__ import annotations

# Побочный эффект: регистрация встроенных тулов до сборки каталога родителя.
from . import builtins as _builtins  # noqa: F401
from .parent_tools import get_parent_tools
from .registry import ToolSpec, register_tool, run_primitive_tool

__all__ = [
    "ToolSpec",
    "get_parent_tools",
    "register_tool",
    "run_primitive_tool",
]
