"""E2E: Open WebUI шлёт RAG во втором system; агент должен оставить один system + JSON + тулы."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import certified_turtles.tools.builtins  # noqa: F401 - register_tool
from certified_turtles.agents.json_agent_protocol import message_text_content
from certified_turtles.agents.loop import run_agent_chat
from certified_turtles.services.message_normalize import normalize_chat_messages


def _file_id_from_messages(messages: list[dict[str, Any]]) -> str | None:
    for m in messages:
        raw = message_text_content(m)
        if "file_id=" not in raw:
            continue
        mm = re.search(r'file_id="([^"]+)"', raw)
        if mm:
            return mm.group(1)
    return None


def _mk_completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _FakeMWS:
    def __init__(self) -> None:
        self.n = 0
        self.snapshots: list[list[dict[str, Any]]] = []

    def chat_completions(self, model: str, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        self.n += 1
        self.snapshots.append(copy.deepcopy(messages))
        fid = _file_id_from_messages(messages)
        if self.n == 1:
            return _mk_completion("Найден 1 источник. Даты: 2026-01-01 и 2026-01-02.")

        if self.n == 2 and fid:
            body: dict[str, Any] = {
                "assistant_markdown": "",
                "calls": [{"name": "workspace_file_path", "arguments": {"file_id": fid}}],
            }
            return _mk_completion(json.dumps(body, ensure_ascii=False))

        if self.n == 3:
            return _mk_completion(
                json.dumps(
                    {"assistant_markdown": "## Итог\nДанные обработаны через тул.", "calls": []},
                    ensure_ascii=False,
                )
            )

        return _mk_completion(json.dumps({"assistant_markdown": "unexpected round", "calls": []}))


def test_single_system_after_merge_and_tool_round_after_repair(monkeypatch, tmp_path):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "up"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "gen"))

    csv = "date,region,revenue_rub\n2026-01-01,A,100\n2026-01-02,B,200\n"
    rag_sys = (
        "### Task: Respond to the user query using the provided context.\n\n"
        f'<source id="1" name="sample.csv">{csv}</source>\n'
    )
    msgs = normalize_chat_messages(
        [
            {"role": "system", "content": rag_sys},
            {"role": "user", "content": "Query: проведи аналитику"},
        ]
    )

    fake = _FakeMWS()
    out = run_agent_chat(fake, "mws-gpt-alpha", msgs, max_tool_rounds=8)

    assert fake.n >= 3, f"ожидались ≥3 вызова MWS, было {fake.n}"

    first_req = fake.snapshots[0]
    system_roles = [m for m in first_req if m.get("role") == "system"]
    assert len(system_roles) == 1
    s0 = system_roles[0]["content"]
    assert isinstance(s0, str)
    assert "Приоритет над вторичными" in s0
    assert "Контекст и инструкции чата" in s0
    assert "file_id=" in s0 or "[CT: RAG-источник" in s0

    visible = out["completion"]["choices"][0]["message"]["content"]
    assert "Итог" in visible or "обработаны" in visible
