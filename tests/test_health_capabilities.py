from __future__ import annotations

import json

from fastapi.testclient import TestClient

from certified_turtles.agents.json_agent_protocol import build_protocol_system_message
from certified_turtles.main import app
from certified_turtles.tools.registry import openai_tools_for_names


def test_health_includes_google_docs_capability():
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    cap = body.get("capabilities") or {}
    assert "google_docs" in cap
    g = cap["google_docs"]
    assert "google_docs_ready" in g
    assert "service_account_client_email" in g
    assert "google_python_packages_installed" in g
    assert g.get("public_read_by_link_supported") is True


def test_protocol_system_includes_google_docs_guide():
    tools = openai_tools_for_names(("google_docs_read",))
    text = build_protocol_system_message(tools)
    assert "Google Docs" in text
    assert "google_docs_read" in text
    assert "google_docs_append" in text or "Тулы:" in text


def test_protocol_without_google_docs_has_no_extra_google_block():
    tools = openai_tools_for_names(("web_search",))
    text = build_protocol_system_message(tools)
    assert "web_search" in text
    assert "=== Google Docs" not in text
