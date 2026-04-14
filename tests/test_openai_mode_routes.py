from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from certified_turtles.chat_modes import resolve_mode_path_segment
from certified_turtles.main import app


def test_resolve_mode_path_accepts_alias_deep():
    assert resolve_mode_path_segment("deep") == "deep_research"


def test_resolve_mode_path_rejects_bad():
    with pytest.raises(ValueError):
        resolve_mode_path_segment("nope")


@pytest.fixture
def client():
    return TestClient(app)


def test_mode_models_404_unknown(client: TestClient):
    r = client.get("/v1/m/not_a_mode/models")
    assert r.status_code == 404


def test_mode_chat_requires_api_key_or_returns_503(client: TestClient, monkeypatch):
    monkeypatch.delenv("MWS_API_KEY", raising=False)
    monkeypatch.delenv("MWS_GPT_API_KEY", raising=False)
    r = client.post(
        "/v1/m/deep_research/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code in (503, 400)


def test_list_models_no_merge_by_default(client: TestClient, monkeypatch):
    """Без CT_LIST_MODE_VARIANTS список не размножается."""
    monkeypatch.setenv("MWS_API_KEY", "test-key-for-list")
    monkeypatch.delenv("CT_LIST_MODE_VARIANTS", raising=False)
    from certified_turtles.api import openai_proxy as op

    class Fake:
        def list_models(self) -> dict:
            return {"object": "list", "data": [{"id": "only-one", "object": "model"}]}

    monkeypatch.setattr(op, "_service", lambda: Fake())

    r = client.get("/v1/models")
    assert r.status_code == 200
    data = r.json().get("data") or []
    ids = [x["id"] for x in data]
    assert ids == ["only-one"]


def test_list_models_merge_when_env(client: TestClient, monkeypatch):
    monkeypatch.setenv("MWS_API_KEY", "test-key-for-list")
    monkeypatch.setenv("CT_LIST_MODE_VARIANTS", "1")
    from certified_turtles.api import openai_proxy as op

    class Fake:
        def list_models(self) -> dict:
            return {"object": "list", "data": [{"id": "only-one", "object": "model"}]}

    monkeypatch.setattr(op, "_service", lambda: Fake())

    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = [x["id"] for x in r.json().get("data") or []]
    assert "only-one" in ids
    assert "deep_research::only-one" in ids
