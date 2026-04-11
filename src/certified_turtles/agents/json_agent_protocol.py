"""
Единый JSON-протокол ответа ассистента (MWS / модели без нативного tool calling).

Все сообщения assistant в агент-цикле с тулами — строго один JSON-объект с фиксированными ключами.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def message_text_content(msg: dict[str, Any]) -> str:
    """Текст из message.content (строка или мультимодальные части)."""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        chunks: list[str] = []
        for p in c:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("text", "input_text"):
                k = "text" if "text" in p else "input_text"
                chunks.append(str(p.get(k) or ""))
        return "\n".join(chunks)
    if c is None:
        return ""
    return str(c)


# Сообщения user с этим префиксом — служебные (результаты тулов), не текст пользователя чата.
PROTOCOL_USER_PREFIX = "[CT_PROTO_JSON]"

# Зашитый контракт (ключи и смысл полей — не переименовывать без правок парсера и тестов).
PROTOCOL_SPEC = r"""
ОБЯЗАТЕЛЬНЫЙ ФОРМАТ КАЖДОГО ТВОЕГО ОТВЕТА (role=assistant):
Ровно один JSON-объект. Без текста до первого «{» и после последнего «}». Без Markdown-ограждений ```.

Структура (все ключи верхнего уровня обязательны, порядок любой):
{
  "assistant_markdown": "<строка: итог для пользователя; при вызове тулов можно временно \"\">",
  "calls": [
    {"name": "<имя_функции_из_каталога>", "arguments": { } }
  ]
}

Правила:
- "calls": [] — когда инструменты не нужны; тогда "assistant_markdown" должен содержать полный ответ пользователю.
- Если нужны инструменты — заполни "calls" одним или несколькими объектами; "arguments" — объект (не строка), по схеме параметров функции из каталога.
- После служебного сообщения пользователя с префиксом [CT_PROTO_JSON] придут результаты тулов — снова ответь ОДНИМ JSON того же вида.

Пример вызова тула:
{"assistant_markdown":"","calls":[{"name":"mws_list_models","arguments":{}}]}

Пример финала:
{"assistant_markdown":"Готово: список моделей выше.","calls":[]}
""".strip()


def _catalog_block(tool_list: list[dict[str, Any]]) -> str:
    lines: list[str] = ["КАТАЛОГ ФУНКЦИЙ (поле name в calls должно совпадать дословно):"]
    for t in tool_list:
        if t.get("type") != "function":
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str):
            continue
        desc = fn.get("description") if isinstance(fn.get("description"), str) else ""
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        lines.append(f"- {name}")
        if desc.strip():
            lines.append(f"  описание: {desc.strip()}")
        lines.append(f"  parameters (JSON Schema): {json.dumps(params, ensure_ascii=False)}")
    return "\n".join(lines)


def build_protocol_system_message(tool_list: list[dict[str, Any]]) -> str:
    return "\n\n".join([PROTOCOL_SPEC, _catalog_block(tool_list)])


def _extract_json_object_string(raw: str) -> str | None:
    s = raw.strip()
    if not s:
        return None
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", s, re.IGNORECASE)
    if m:
        inner = m.group(1).strip()
        if inner.startswith("{"):
            return inner
    lb = s.find("{")
    rb = s.rfind("}")
    if lb != -1 and rb != -1 and rb > lb:
        return s[lb : rb + 1]
    return None


def parse_agent_response(raw: str) -> dict[str, Any] | None:
    """Разбор ответа ассистента по протоколу; None если не JSON или неверная структура."""
    blob = _extract_json_object_string(raw)
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        logger.debug("parse_agent_response: JSONDecodeError")
        return None
    if not isinstance(data, dict):
        return None
    if "assistant_markdown" not in data or "calls" not in data:
        return None
    am = data.get("assistant_markdown")
    if not isinstance(am, str):
        return None
    calls = data.get("calls")
    if not isinstance(calls, list):
        return None
    norm_calls: list[dict[str, Any]] = []
    for i, c in enumerate(calls[:32]):
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        args = c.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {"_raw_arguments": (args or "")[:2000]}
        if not isinstance(args, dict):
            args = {}
        norm_calls.append({"name": name.strip(), "arguments": args})
    return {"assistant_markdown": am, "calls": norm_calls}


def tool_outputs_user_message(calls: list[dict[str, Any]], outputs: list[str]) -> str:
    payload = {
        "tool_outputs": [
            {"name": calls[i]["name"], "output": outputs[i]}
            for i in range(min(len(calls), len(outputs)))
        ]
    }
    return f"{PROTOCOL_USER_PREFIX}\n{json.dumps(payload, ensure_ascii=False)}"


def extract_user_visible_assistant_text(content: str) -> str:
    """Для UI и под-агента: markdown из протокола или исходная строка."""
    parsed = parse_agent_response(content)
    if parsed is None:
        return content
    return parsed["assistant_markdown"]


def patch_completion_assistant_markdown(completion: dict[str, Any], markdown: str) -> dict[str, Any]:
    """Копия completion с content = markdown для Open WebUI."""
    out = copy.deepcopy(completion)
    choices = out.get("choices")
    if isinstance(choices, list) and choices:
        ch0 = choices[0]
        if isinstance(ch0, dict):
            msg = ch0.get("message")
            if isinstance(msg, dict):
                msg = dict(msg)
                msg["content"] = markdown
                ch0 = dict(ch0)
                ch0["message"] = msg
                choices = [ch0] + list(choices[1:])
                out["choices"] = choices
    return out
