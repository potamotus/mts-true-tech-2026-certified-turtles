"""Импортируйте подмодули, чтобы зарегистрировать встроенные тулы (побочный эффект: register_tool)."""

from __future__ import annotations

from . import fetch_url as fetch_url  # noqa: F401
from . import web_search as web_search  # noqa: F401
