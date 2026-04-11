from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class RequestContext:
    session_id: str
    scope_id: str


_LOCAL = threading.local()


def current_request_context() -> RequestContext | None:
    return getattr(_LOCAL, "ctx", None)


@contextmanager
def use_request_context(ctx: RequestContext):
    prev = current_request_context()
    _LOCAL.ctx = ctx
    try:
        yield
    finally:
        if prev is None:
            try:
                delattr(_LOCAL, "ctx")
            except AttributeError:
                pass
        else:
            _LOCAL.ctx = prev
