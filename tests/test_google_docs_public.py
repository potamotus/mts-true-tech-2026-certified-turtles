from __future__ import annotations

import json

from certified_turtles.tools.registry import run_primitive_tool


def test_google_docs_read_public_when_no_service_account(monkeypatch):
    monkeypatch.setattr(
        "certified_turtles.tools.builtins.google_docs._build_docs_service",
        lambda: (None, "no service account"),
    )

    def fake_fetch(url: str, *, max_chars: int = 8000, timeout: int = 15):
        assert "/export?format=txt" in url
        return {"url": url, "title": "", "text": "public body"}

    monkeypatch.setattr("certified_turtles.tools.fetch_url.fetch_url_text", fake_fetch)

    raw = run_primitive_tool("google_docs_read", {"document_id": "https://docs.google.com/document/d/abc123/edit"})
    data = json.loads(raw)
    assert data.get("text") == "public body"
    assert data.get("via") == "public_link_export"
