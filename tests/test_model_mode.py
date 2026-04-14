from __future__ import annotations

from certified_turtles.model_mode import apply_virtual_model_to_body, merge_virtual_models_openai_payload, split_virtual_model


def test_split_virtual_model():
    assert split_virtual_model("deep_research::gpt-test") == ("gpt-test", "deep_research")
    assert split_virtual_model("foo::bar") == ("foo::bar", None)
    assert split_virtual_model("gpt-4") == ("gpt-4", None)
    assert split_virtual_model("deep_research::a::b") == ("a::b", "deep_research")


def test_apply_virtual_sets_ct_mode():
    body: dict = {"model": "deep_research::my-model"}
    apply_virtual_model_to_body(body)
    assert body["model"] == "my-model"
    assert body["ct_mode"] == "deep_research"


def test_apply_explicit_ct_mode_only_strips_model():
    body = {"model": "deep_research::x", "ct_mode": "writer"}
    apply_virtual_model_to_body(body)
    assert body["model"] == "x"
    assert body["ct_mode"] == "writer"


def test_merge_models_list():
    payload = {
        "object": "list",
        "data": [
            {"id": "alpha", "object": "model"},
        ],
    }
    out = merge_virtual_models_openai_payload(payload)
    assert isinstance(out, dict)
    ids = [x["id"] for x in out["data"]]
    assert ids.count("alpha") == 1
    assert "deep_research::alpha" in ids
    assert "research::alpha" in ids


def test_merge_skips_already_prefixed_base():
    """Id с :: не размножаем (избегаем deep_research::deep_research::…)."""
    payload = {"data": [{"id": "deep_research::x", "object": "model"}]}
    out = merge_virtual_models_openai_payload(payload)
    ids = [x["id"] for x in out["data"]]
    assert ids == ["deep_research::x"]
