from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class RequestContext:
    session_id: str
    scope_id: str
    file_state_namespace: str | None = None


_LOCAL = threading.local()


def current_request_context() -> RequestContext | None:
    return getattr(_LOCAL, "ctx", None)


@contextmanager
def use_request_context(ctx: RequestContext):
    prev = push_request_context(ctx)
    try:
        yield
    finally:
        pop_request_context(prev)


def push_request_context(ctx: RequestContext | None) -> RequestContext | None:
    """Set request context and return the previous one (for generators that can't use ``with``)."""
    prev = current_request_context()
    if ctx is not None:
        _LOCAL.ctx = ctx
    return prev


def pop_request_context(prev: RequestContext | None) -> None:
    """Restore a previous request context returned by :func:`push_request_context`."""
    if prev is None:
        try:
            delattr(_LOCAL, "ctx")
        except AttributeError:
            pass
    else:
        _LOCAL.ctx = prev
