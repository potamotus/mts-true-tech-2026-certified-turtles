from __future__ import annotations

from certified_turtles.agents.execute_python_intent import (
    llm_should_skip_execute_python,
    parse_skip_execute_python_flag,
)


def test_parse_skip_execute_python_flag_plain_json():
    assert parse_skip_execute_python_flag('{"skip_execute_python": true}') is True
    assert parse_skip_execute_python_flag('{"skip_execute_python": false}') is False


def test_parse_skip_execute_python_flag_fenced():
    raw = '```json\n{"skip_execute_python": true}\n```'
    assert parse_skip_execute_python_flag(raw) is True


def test_parse_skip_execute_python_flag_noise_prefix():
    raw = 'Пояснение\n{"skip_execute_python": false}'
    assert parse_skip_execute_python_flag(raw) is False


def test_llm_should_skip_execute_python_disabled(monkeypatch):
    monkeypatch.setenv("CT_EXECUTE_PYTHON_INTENT_LLM", "0")
    class C:
        def chat_completions(self, *a, **k):
            raise AssertionError("LLM should not be called when intent LLM disabled")

    assert llm_should_skip_execute_python(C(), "m", "напиши код") is False


def test_llm_should_skip_execute_python_empty_user():
    class C:
        def chat_completions(self, *a, **k):
            raise AssertionError("no call")

    assert llm_should_skip_execute_python(C(), "m", "") is False
    assert llm_should_skip_execute_python(C(), "m", "   ") is False


def test_llm_should_skip_execute_python_uses_response(monkeypatch):
    monkeypatch.delenv("CT_EXECUTE_PYTHON_INTENT_LLM", raising=False)

    class C:
        def chat_completions(self, model, messages, **kwargs):
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": '{"skip_execute_python": true}'}}
                ]
            }

    assert llm_should_skip_execute_python(C(), "m", "покажи код quicksort") is True


def test_llm_should_skip_execute_python_parse_fail_allows_run(monkeypatch):
    monkeypatch.delenv("CT_EXECUTE_PYTHON_INTENT_LLM", raising=False)

    class C:
        def chat_completions(self, model, messages, **kwargs):
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "not json"}}
                ]
            }

    assert llm_should_skip_execute_python(C(), "m", "anything") is False
