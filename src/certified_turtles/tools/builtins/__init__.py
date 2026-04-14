"""Импортируйте подмодули, чтобы зарегистрировать встроенные тулы (побочный эффект: register_tool)."""

from __future__ import annotations

from . import execute_python as execute_python  # noqa: F401
from . import file_ops as file_ops  # noqa: F401
from . import fetch_url as fetch_url  # noqa: F401
from . import generate_image as generate_image  # noqa: F401
from . import generate_presentation as generate_presentation  # noqa: F401
from . import google_docs as google_docs  # noqa: F401
from . import read_workspace_file as read_workspace_file  # noqa: F401
from . import transcribe_workspace_audio as transcribe_workspace_audio  # noqa: F401
from . import web_search as web_search  # noqa: F401
from . import workspace_file_path as workspace_file_path  # noqa: F401

from . import mws_tables as mws_tables  # noqa: F401
