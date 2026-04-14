from __future__ import annotations

import json

from certified_turtles.agents.registry import (
    AUTO_DREAM_AGENT_ID,
    CODER_AGENT_ID,
    DEEP_RESEARCH_AGENT_ID,
    MEMORY_EXTRACTOR_AGENT_ID,
    MEMORY_TESTER_AGENT_ID,
    SESSION_MEMORY_AGENT_ID,
    get_subagent,
)
from certified_turtles.tools.parent_tools import get_parent_tools
from certified_turtles.tools.registry import list_primitive_tool_names, run_primitive_tool


def test_new_primitives_registered():
    names = list_primitive_tool_names()
    assert "web_search" in names
    assert "fetch_url" in names
    assert "generate_image" in names
    assert "generate_presentation" in names
    assert "read_workspace_file" in names
    assert "transcribe_workspace_audio" in names
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
        "transcribe_workspace_audio",
        "execute_python",
        "google_docs_read",
        "google_docs_append",
    ):
        assert expected in tool_names
    assert f"agent_{DEEP_RESEARCH_AGENT_ID}" in tool_names
    assert f"agent_{CODER_AGENT_ID}" in tool_names
    assert f"agent_{MEMORY_EXTRACTOR_AGENT_ID}" in tool_names
    assert f"agent_{SESSION_MEMORY_AGENT_ID}" in tool_names
    assert f"agent_{AUTO_DREAM_AGENT_ID}" in tool_names
    assert f"agent_{MEMORY_TESTER_AGENT_ID}" in tool_names


def test_deep_research_subagent_uses_gpt_researcher_not_tool_loop():
    spec = get_subagent(DEEP_RESEARCH_AGENT_ID)
    assert spec is not None
    assert spec.tool_names == ()
    assert spec.max_inner_rounds == 1


def test_coder_subagent_has_python_tools():
    spec = get_subagent(CODER_AGENT_ID)
    assert spec is not None
    assert "execute_python" in spec.tool_names
    assert "read_workspace_file" in spec.tool_names


def test_memory_subagents_have_claude_like_file_tools():
    for agent_id in (
        MEMORY_EXTRACTOR_AGENT_ID,
        SESSION_MEMORY_AGENT_ID,
        AUTO_DREAM_AGENT_ID,
        MEMORY_TESTER_AGENT_ID,
    ):
        spec = get_subagent(agent_id)
        assert spec is not None
        assert any(name.startswith("file_") or name.endswith("_search") for name in spec.tool_names)


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


def test_generate_presentation_section_and_thanks_allow_empty_bullets(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "http://test.local")
    out = run_primitive_tool(
        "generate_presentation",
        {
            "title": "Demo",
            "subtitle": "2026",
            "slides": [
                {"title": "Введение", "kind": "section", "bullets": []},
                {"title": "Детали", "bullets": ["a", "b"]},
                {"title": "Спасибо", "kind": "thanks", "bullets": []},
            ],
        },
    )
    data = json.loads(out)
    assert "error" not in data
    assert data["slide_count"] == 4


def test_generate_presentation_image_requires_url(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path))
    out = run_primitive_tool(
        "generate_presentation",
        {
            "title": "x",
            "slides": [{"title": "Pic", "kind": "image", "bullets": ["описание"]}],
        },
    )
    assert "error" in json.loads(out)


def test_generate_presentation_image_slide_allows_empty_bullets(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "http://test.local")
    tiny_png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500001d0d4e120000000049454e44ae426082"
    )

    def _fake_download(url: str, **kwargs):
        assert "http" in url
        return tiny_png

    monkeypatch.setattr(
        "certified_turtles.tools.presentation._download_image_bytes",
        _fake_download,
    )
    out = run_primitive_tool(
        "generate_presentation",
        {
            "title": "Deck",
            "slides": [
                {
                    "title": "Скриншот",
                    "kind": "image",
                    "image_url": "https://example.com/x.png",
                    "bullets": [],
                },
            ],
        },
    )
    data = json.loads(out)
    assert "error" not in data, data
    assert data["slide_count"] == 2


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


def test_web_search_returns_structured_json(monkeypatch):
    """Успешная выдача — JSON с results и summary (удобно парсить и читать)."""
    import certified_turtles.tools.builtins.web_search as ws_builtin

    monkeypatch.setattr(
        ws_builtin,
        "duckduckgo_text_search",
        lambda q, max_results=5: [
            {"title": "T", "href": "https://a.example", "body": "snippet"},
        ],
    )
    out = run_primitive_tool("web_search", {"query": "test query", "max_results": 3})
    data = json.loads(out)
    assert data.get("query") == "test query"
    assert data.get("count") == 1
    assert len(data.get("results") or []) == 1
    assert "summary" in data
    assert "T" in data["summary"]


def test_execute_python_allows_urllib_and_http_client(monkeypatch, tmp_path):
    """HTTP из кода: urllib/http.client/ssl и requests (белый список)."""
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "gen"))
    code = (
        "import http.client\n"
        "import ssl\n"
        "import urllib.parse\n"
        "import urllib.request\n"
        "import requests\n"
        "print('imports_ok', requests.__version__)\n"
    )
    out = run_primitive_tool("execute_python", {"code": code})
    data = json.loads(out)
    assert data.get("returncode") == 0, data
    assert "imports_ok" in (data.get("stdout") or "")
