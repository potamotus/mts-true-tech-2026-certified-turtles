from __future__ import annotations

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


def test_normalize_chat_messages():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    out = normalize_chat_messages(msgs)
    assert out[0]["content"] == "hi"
