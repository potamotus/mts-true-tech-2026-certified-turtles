from __future__ import annotations

import base64

import pytest

from certified_turtles.services.message_normalize import normalize_chat_messages, normalize_message_content


def test_normalize_plain_string_unchanged():
    assert normalize_message_content("hello") == "hello"


def test_normalize_text_parts_merged_to_string():
    content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert normalize_message_content(content) == "a\nb"


def test_normalize_image_url_preserved_in_list():
    content = [{"type": "text", "text": "see"}, {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]
    out = normalize_message_content(content)
    assert isinstance(out, list)
    assert out[-1]["type"] == "image_url"


def test_normalize_input_image_maps_to_image_url():
    content = [{"type": "input_image", "input_image": {"url": "https://example.com/z.jpg"}}]
    out = normalize_message_content(content)
    assert isinstance(out, list)
    assert out[0]["type"] == "image_url"
    assert "example.com" in out[0]["image_url"]["url"]


def test_normalize_chat_messages():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    out = normalize_chat_messages(msgs)
    assert out[0]["content"] == "hi"


@pytest.fixture
def uploads_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "up"))
    return tmp_path / "up"


def test_file_attachment_data_url_saved_to_workspace(uploads_tmp):
    raw = b"a,b\n1,2\n"
    b64 = base64.b64encode(raw).decode()
    content = [
        {"type": "text", "text": "анализ"},
        {
            "type": "file",
            "filename": "demo.csv",
            "file": {"data": f"data:text/csv;base64,{b64}"},
        },
    ]
    out = normalize_message_content(content)
    assert isinstance(out, str)
    assert "file_id:" in out
    assert "demo.csv" in out or "оригинальное имя" in out
    saved = list(uploads_tmp.iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == raw


def test_file_attachment_raw_base64_saved(uploads_tmp):
    raw = b"x,y\n3,4\n"
    b64 = base64.b64encode(raw).decode()
    content = [{"type": "file", "filename": "t.csv", "file": b64}]
    out = normalize_message_content(content)
    assert isinstance(out, str)
    assert "file_id:" in out


def test_audio_attachment_saved_and_hints_transcribe_tool(uploads_tmp):
    raw = b"RIFF" + b"\x00" * 32
    b64 = base64.b64encode(raw).decode()
    content = [
        {"type": "file", "filename": "note.mp3", "mime_type": "audio/mpeg", "file": {"data": f"data:audio/mpeg;base64,{b64}"}},
    ]
    out = normalize_message_content(content)
    assert isinstance(out, str)
    assert "file_id:" in out
    assert "transcribe_workspace_audio" in out
    saved = list(uploads_tmp.iterdir())
    assert len(saved) == 1
    assert saved[0].suffix == ".mp3"


def test_input_audio_decoded_as_file(uploads_tmp):
    raw = b"fake-wav-bytes-" + b"\x00" * 32
    b64 = base64.b64encode(raw).decode()
    content = [{"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}]
    out = normalize_message_content(content)
    assert isinstance(out, str)
    assert "file_id:" in out
    assert "transcribe_workspace_audio" in out


def test_open_webui_rag_source_hydrated(uploads_tmp):
    body = "a,b\n1,2\n"
    text = (
        '### Context\n<source id="1" name="demo.csv">'
        f"{body}"
        "</source>\nQuery: analyze"
    )
    out = normalize_message_content(text)
    assert "file_id=" in out
    assert "[CT: RAG-источник" in out
    saved = list(uploads_tmp.iterdir())
    assert len(saved) == 1
    assert "1,2" in saved[0].read_text(encoding="utf-8")


def test_open_webui_rag_ignores_instruction_example_source(uploads_tmp):
    text = (
        'Use tags like <source id="1"> in examples only.\n'
        "<context>"
        '<source id="7" name="demo.csv">date: 2026-01-07\nregion: Москва\nsales: 100</source>'
        "</context>\n"
        "Query: analyze"
    )
    out = normalize_message_content(text)
    assert 'Use tags like <source id="1">' in out
    assert 'file_id="' in out
    saved = list(uploads_tmp.iterdir())
    assert len(saved) == 1
    assert saved[0].suffix == ".csv"
    assert "date,region,sales" in saved[0].read_text(encoding="utf-8")
