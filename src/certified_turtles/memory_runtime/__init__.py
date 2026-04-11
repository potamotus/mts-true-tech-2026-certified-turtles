from __future__ import annotations

from typing import TYPE_CHECKING

from .request_context import RequestContext, current_request_context, use_request_context

if TYPE_CHECKING:
    from .manager import ClaudeLikeMemoryRuntime


def runtime_from_env():
    from .manager import runtime_from_env as _runtime_from_env

    return _runtime_from_env()

__all__ = [
    "ClaudeLikeMemoryRuntime",
    "RequestContext",
    "current_request_context",
    "runtime_from_env",
    "use_request_context",
]
