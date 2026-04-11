from __future__ import annotations

import json

from certified_turtles.agents.registry import DEEP_RESEARCH_AGENT_ID, get_subagent
from certified_turtles.tools.parent_tools import get_parent_tools
from certified_turtles.tools.registry import list_primitive_tool_names, run_primitive_tool


def test_new_primitives_registered():
    names = list_primitive_tool_names()
    assert "web_search" in names
    assert "fetch_url" in names
    assert "generate_image" in names
    assert "generate_presentation" in names
    assert "read_workspace_file" in names
    assert "execute_python" in names
    assert "google_docs_read" in names
    assert "google_docs_append" in names


def test_parent_tools_expose_all():
    tool_names = [t["function"]["name"] for t in get_parent_tools()]
    for expected in (
        "web_search",
        "fetch_url",
        "generate_image",
        "generate_presentation",
        "read_workspace_file",
        "execute_python",
        "google_docs_read",
        "google_docs_append",
    ):
        assert expected in tool_names
    assert f"agent_{DEEP_RESEARCH_AGENT_ID}" in tool_names


def test_deep_research_subagent_has_research_tools():
    spec = get_subagent(DEEP_RESEARCH_AGENT_ID)
    assert spec is not None
    assert "web_search" in spec.tool_names
    assert "fetch_url" in spec.tool_names


def test_generate_image_returns_pollinations_url():
    out = run_primitive_tool("generate_image", {"prompt": "a red turtle with glasses"})
    data = json.loads(out)
    assert data["url"].startswith("https://image.pollinations.ai/prompt/")
    assert "a%20red%20turtle" in data["url"]
    assert data["markdown"].startswith("![")
    assert 256 <= data["width"] <= 1536


def test_generate_image_rejects_empty_prompt():
    out = run_primitive_tool("generate_image", {"prompt": ""})
    assert "error" in json.loads(out)


def test_generate_presentation_writes_pptx(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "http://test.local")
    out = run_primitive_tool(
        "generate_presentation",
        {
            "title": "Hackathon Pitch",
            "subtitle": "MTS True Tech 2026",
            "slides": [
                {"title": "Проблема", "bullets": ["фрагментация", "нет памяти"]},
                {"title": "Решение", "bullets": ["единый фасад", "tools", "авто модель"]},
            ],
        },
    )
    data = json.loads(out)
    assert data["filename"].endswith(".pptx")
    assert data["download_url"].startswith("http://test.local/files/")
    file_path = tmp_path / data["filename"]
    assert file_path.is_file()
    # .pptx — ZIP, первые байты "PK"
    assert file_path.read_bytes()[:2] == b"PK"
    assert data["slide_count"] == 3  # титульный + 2 контентных


def test_generate_presentation_rejects_empty_slides(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path))
    out = run_primitive_tool(
        "generate_presentation",
        {"title": "x", "slides": []},
    )
    assert "error" in json.loads(out)


def test_files_route_serves_generated_pptx(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "http://test.local")
    out = run_primitive_tool(
        "generate_presentation",
        {
            "title": "Smoke",
            "slides": [{"title": "One", "bullets": ["a", "b"]}],
        },
    )
    data = json.loads(out)
    filename = data["filename"]

    from fastapi.testclient import TestClient

    from certified_turtles.main import app

    client = TestClient(app)
    r = client.get(f"/files/{filename}")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"

    r_bad = client.get("/files/../etc/passwd")
    assert r_bad.status_code in (400, 404)


def test_web_search_refuses_url_query():
    out = run_primitive_tool("web_search", {"query": "https://example.com"})
    data = json.loads(out)
    assert data.get("error") == "bad_query"
