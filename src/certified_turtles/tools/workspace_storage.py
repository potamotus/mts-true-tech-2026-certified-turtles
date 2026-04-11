from __future__ import annotations

import os
from pathlib import Path


def uploads_dir() -> Path:
    """Каталог пользовательских загрузок (том в compose: UPLOADS_DIR)."""
    root = os.environ.get("UPLOADS_DIR", "/tmp/certified_turtles_uploads")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path
