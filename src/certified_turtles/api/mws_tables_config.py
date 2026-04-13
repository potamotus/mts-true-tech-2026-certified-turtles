"""GET/POST /api/v1/mws-tables/config — настройка MWS Tables из UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from certified_turtles.mws_tables import client as tables_client

router = APIRouter(prefix="/mws-tables", tags=["mws-tables"])

def _config_file() -> Path:
    return Path(os.environ.get("CT_DATA_DIR", str(Path(__file__).resolve().parent.parent.parent.parent))) / ".mws_tables_config.json"


def _load_persisted() -> None:
    """Load saved config into os.environ on startup (if not already set)."""
    p = _config_file()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return
    for key in ("MWS_TABLES_API_TOKEN", "MWS_TABLES_API_BASE"):
        val = data.get(key, "")
        if val and not os.environ.get(key):
            os.environ[key] = val


def _save_persisted() -> None:
    """Persist current config to disk."""
    data = {
        "MWS_TABLES_API_TOKEN": os.environ.get("MWS_TABLES_API_TOKEN", ""),
        "MWS_TABLES_API_BASE": os.environ.get("MWS_TABLES_API_BASE", ""),
    }
    try:
        p = _config_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except OSError:
        pass


_load_persisted()


@router.get("/config")
def get_config() -> dict[str, Any]:
    return {
        "MWS_TABLES_API_TOKEN": os.environ.get("MWS_TABLES_API_TOKEN", ""),
        "MWS_TABLES_API_BASE": os.environ.get("MWS_TABLES_API_BASE", tables_client.DEFAULT_BASE_URL),
        "configured": tables_client.is_configured(),
    }


@router.post("/config")
def update_config(body: dict[str, Any]) -> dict[str, Any]:
    token = body.get("MWS_TABLES_API_TOKEN", "").strip()
    base = body.get("MWS_TABLES_API_BASE", "").strip()

    if token:
        os.environ["MWS_TABLES_API_TOKEN"] = token
    if base:
        os.environ["MWS_TABLES_API_BASE"] = base

    # Reset singleton so next call picks up new creds
    tables_client._client = None

    _save_persisted()

    return {
        "MWS_TABLES_API_TOKEN": os.environ.get("MWS_TABLES_API_TOKEN", ""),
        "MWS_TABLES_API_BASE": os.environ.get("MWS_TABLES_API_BASE", tables_client.DEFAULT_BASE_URL),
        "configured": tables_client.is_configured(),
    }


@router.post("/verify")
def verify_connection(body: dict[str, Any]) -> dict[str, Any]:
    """Проверяет подключение к MWS Tables с указанным токеном."""
    token = body.get("token", "").strip()
    base = body.get("base", "").strip() or tables_client.DEFAULT_BASE_URL
    if not token:
        return {"ok": False, "error": "Токен не указан"}
    try:
        c = tables_client.MWSTablesClient(token=token, base_url=base)
        resp = c.list_spaces()
        spaces = resp.get("data", {}).get("spaces", [])
        return {"ok": True, "spaces": spaces}
    except tables_client.MWSTablesError as e:
        return {"ok": False, "error": str(e), "status": e.status}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
