"""
Единый JSON-протокол ответа ассистента (MWS / модели без нативного tool calling).

Все сообщения assistant в агент-цикле с тулами — строго один JSON-объект с фиксированными ключами.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any

from certified_turtles.prompts import load_prompt

logger = logging.getLogger(__name__)

# Модели с «размышлением» часто оборачивают его в теги; мешают извлечению JSON.
_STRIP_REASONING_BLOCKS = re.compile(
    r"(?:"
    r"<(?:redacted_)?thinking>[\s\S]*?</(?:redacted_)?thinking>"
    r"|<think>[\s\S]*?</think>"
    r"|<redacted_reasoning>[\s\S]*?</redacted_reasoning>"
    r")",
    re.IGNORECASE,
)


def _strip_model_reasoning_noise(raw: str) -> str:
    s = _STRIP_REASONING_BLOCKS.sub("", raw)
    return s.strip()


def _iter_balanced_json_objects(s: str) -> list[str]:
    """Все подстроки верхнего уровня `{...}` с учётом строк и экранирования в JSON."""
    n = len(s)
    out: list[str] = []
    i = 0
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        start = i
        depth = 0
        in_string = False
        escape = False
        j = i
        closed = False
        while j < n:
            c = s[j]
            if escape:
                escape = False
                j += 1
                continue
            if in_string:
                if c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                j += 1
                continue
            if c == '"':
                in_string = True
                j += 1
                continue
            if c == "{":
                depth += 1
                j += 1
                continue
            if c == "}":
                depth -= 1
                j += 1
                if depth == 0:
                    out.append(s[start:j])
                    i = j
                    closed = True
                    break
                continue
            j += 1
        if not closed:
            i = start + 1
    return out


def _extract_protocol_json_candidates(raw: str) -> list[str]:
    """Возможные JSON-тела протокола: блоки ```json, затем сбалансированные объекты (с конца к началу)."""
    s = _strip_model_reasoning_noise(raw)
    candidates: list[str] = []
    seen: set[str] = set()

    def add(blob: str) -> None:
        b = blob.strip()
        if not b or b in seen:
            return
        seen.add(b)
        candidates.append(b)

    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", s, re.IGNORECASE):
        inner = m.group(1).strip()
        if inner.startswith("{"):
            add(inner)
    balanced = _iter_balanced_json_objects(s)
    for obj in reversed(balanced):
        add(obj)
    return candidates


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

# Один «ремонтный» user-ход при ответе модели без парсящегося JSON (иначе тулы не вызываются).
PROTOCOL_JSON_REPAIR_USER = load_prompt("protocol_json_repair_user.txt").strip()

# Зашитый контракт (ключи и смысл полей — не переименовывать без правок парсера и тестов).
PROTOCOL_SPEC = load_prompt("protocol_spec.md").strip()


def _tool_names_in_catalog(tool_list: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for t in tool_list:
        if t.get("type") != "function":
            continue
        fn = t.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return names


def _type_hint_for_schema(spec: dict[str, Any]) -> str:
    t = spec.get("type")
    if t == "array":
        it = spec.get("items")
        if isinstance(it, dict):
            return f"array[{_type_hint_for_schema(it)}]"
        return "array"
    if t == "object":
        nested = spec.get("properties")
        if isinstance(nested, dict) and nested:
            bits: list[str] = []
            for j, (kn, vs) in enumerate(nested.items()):
                if j >= 4:
                    bits.append("…")
                    break
                bits.append(
                    f"{kn}:{_type_hint_for_schema(vs) if isinstance(vs, dict) else 'any'}",
                )
            return "{" + ";".join(bits) + "}"
        return "object"
    if isinstance(t, list):
        return "|".join(str(x) for x in t)
    if t:
        return str(t)
    if "enum" in spec:
        return "enum"
    return "any"


def _parameters_compact_line(params: dict[str, Any]) -> str:
    props = params.get("properties")
    if not isinstance(props, dict):
        return "()"
    req_raw = params.get("required")
    req_set = {str(x) for x in req_raw} if isinstance(req_raw, list) else set()
    parts: list[str] = []
    for i, (name, spec) in enumerate(props.items()):
        if i >= 14:
            parts.append("…")
            break
        if not isinstance(spec, dict):
            continue
        opt = "" if name in req_set else "?"
        parts.append(f"{name}{opt}:{_type_hint_for_schema(spec)}")
    return ", ".join(parts) if parts else "()"


def _catalog_block(tool_list: list[dict[str, Any]], *, compact: bool) -> str:
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
        if compact:
            lines.append(f"  args: {_parameters_compact_line(params)}")
        else:
            lines.append(
                "  parameters (JSON Schema): "
                f"{json.dumps(params, ensure_ascii=False, separators=(',', ':'))}",
            )
    return "\n".join(lines)


def compact_tool_catalog_enabled() -> bool:
    """Меньший системный промпт агента — снижает риск обрыва MWS на больших телах запроса."""
    v = os.environ.get("CT_AGENT_COMPACT_TOOL_CATALOG", "1").strip().lower()
    return v not in ("0", "false", "no", "off", "full")


def build_protocol_system_message(tool_list: list[dict[str, Any]]) -> str:
    parts: list[str] = [PROTOCOL_SPEC, _catalog_block(tool_list, compact=compact_tool_catalog_enabled())]
    names = _tool_names_in_catalog(tool_list)
    if "workspace_file_path" in names:
        parts.append(
            "Open WebUI / RAG: в тексте могут быть теги <source id=\"…\" name=\"…\"> — id там это номер цитаты, "
            "не file_id. Для workspace_file_path бери только file_id из строки с префиксом [CT: RAG-источник …]."
        )
    if any(n.startswith("google_docs_") for n in names):
        from certified_turtles.tools.builtins.google_docs import agent_system_prompt_google_docs_section

        parts.append(agent_system_prompt_google_docs_section())
    return "\n\n".join(parts)


def _normalize_protocol_dict(data: dict[str, Any]) -> dict[str, Any] | None:
    """Проверка и нормализация объекта протокола после json.loads (в т.ч. null от моделей)."""
    if "assistant_markdown" not in data or "calls" not in data:
        return None
    am = data.get("assistant_markdown")
    if am is None:
        am = ""
    elif not isinstance(am, str):
        return None
    calls_raw = data.get("calls")
    if calls_raw is None:
        calls_raw = []
    if not isinstance(calls_raw, list):
        return None
    norm_calls: list[dict[str, Any]] = []
    for c in calls_raw[:32]:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        args = c.get("arguments", {})
        if args is None:
            args = {}
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {"_raw_arguments": (args or "")[:2000]}
        if not isinstance(args, dict):
            args = {}
        norm_calls.append({"name": name.strip(), "arguments": args})
    return {"assistant_markdown": am, "calls": norm_calls}


def _parse_protocol_blob(blob: str) -> dict[str, Any] | None:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        logger.debug("parse_agent_response: JSONDecodeError: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    return _normalize_protocol_dict(data)


def iter_parsed_protocol_payloads(raw: str) -> list[dict[str, Any]]:
    """Все валидные объекты протокола в порядке кандидатов (несколько ```json / несколько объектов в тексте)."""
    out: list[dict[str, Any]] = []
    for blob in _extract_protocol_json_candidates(raw):
        parsed = _parse_protocol_blob(blob)
        if parsed is not None:
            out.append(parsed)
    return out


def _lenient_protocol_payloads(raw: str) -> list[dict[str, Any]]:
    """Декодер JSON по позиции `{`: ловит объект протокола после любого преамбульного текста."""
    s = _strip_model_reasoning_noise(raw)
    dec = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    i = 0
    n = len(s)
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        try:
            _, end = dec.raw_decode(s[i:])
        except json.JSONDecodeError:
            i += 1
            continue
        blob = s[i : i + end].strip()
        if blob in seen:
            i += end
            continue
        seen.add(blob)
        parsed = _parse_protocol_blob(blob)
        if parsed is not None:
            out.append(parsed)
        i += end
    return out


def iter_parsed_protocol_payloads_any(raw: str) -> list[dict[str, Any]]:
    primary = iter_parsed_protocol_payloads(raw)
    if primary:
        return primary
    return _lenient_protocol_payloads(raw)


def parse_failure_max_chars() -> int:
    """Лимит символов для WARNING при parse failed; CT_AGENT_PARSE_FAILURE_LOG_MAX_CHARS=0 — без обрезки."""
    raw = os.environ.get("CT_AGENT_PARSE_FAILURE_LOG_MAX_CHARS")
    if raw is not None and raw.strip() == "0":
        return 10**9
    if raw is None or not str(raw).strip():
        return 100_000
    try:
        n = int(raw.strip())
    except (TypeError, ValueError):
        return 100_000
    if n <= 0:
        return 10**9
    return max(4_000, min(2_000_000, n))


def parse_failure_log_preview(raw: str, *, max_chars: int | None = None) -> str:
    """Сырой ответ для WARNING-логов; по умолчанию длинный лимит (см. parse_failure_max_chars)."""
    if max_chars is None:
        max_chars = parse_failure_max_chars()
    if not raw:
        return "<пусто>"
    s = raw.strip()
    if len(s) <= max_chars:
        return s
    edge = (max_chars - 36) // 2
    return f"{s[:edge]}\n… [обрезано {len(s)} симв., CT_AGENT_PARSE_FAILURE_LOG_MAX_CHARS] …\n{s[-edge:]}"


def diagnose_protocol_parse_failure(raw: str | None) -> str:
    """Текст для логов: почему ответ не распознан как протокол (типы, JSONDecodeError, кандидаты)."""
    lines: list[str] = []
    if raw is None:
        return "content=None"
    s = raw.strip()
    if not s:
        return "пустая строка после strip"
    lines.append(f"длина={len(s)}")
    try:
        root = json.loads(s)
        lines.append(f"json.loads(целиком): ok, тип корня={type(root).__name__}")
        if isinstance(root, dict):
            lines.append(f"  ключи верхнего уровня: {list(root.keys())[:24]}")
            am = root.get("assistant_markdown")
            cl = root.get("calls")
            lines.append(f"  assistant_markdown: type={type(am).__name__} repr={repr(am)[:200]}")
            lines.append(f"  calls: type={type(cl).__name__}")
            norm = _normalize_protocol_dict(root)
            lines.append(f"  _normalize_protocol_dict: {'OK' if norm else 'ОТКЛОНЁН'}")
    except json.JSONDecodeError as e:
        lines.append(f"json.loads(целиком): JSONDecodeError: {e}")

    cands = _extract_protocol_json_candidates(s)
    lines.append(f"кандидатов из _extract_protocol_json_candidates: {len(cands)}")
    for i, blob in enumerate(cands[:6]):
        try:
            json.loads(blob)
        except json.JSONDecodeError as e:
            lines.append(f"  [{i}] len={len(blob)}: JSONDecodeError {e}")
            continue
        pb = _parse_protocol_blob(blob)
        lines.append(f"  [{i}] len={len(blob)}: json ok, parse_blob={'OK' if pb else 'ОТКЛОНЁН'}")

    lenient = _lenient_protocol_payloads(s)
    lines.append(f"_lenient_protocol_payloads: распознано объектов: {len(lenient)}")
    return "\n".join(lines)


def parse_agent_response(raw: str) -> dict[str, Any] | None:
    """Разбор одного ответа ассистента для оркестратора.

    Берём первый объект с непустым `calls` (нужно выполнить тулы). Если таких нет — последний
    валидный объект (итог раунда). Так модель может ошибочно вставить несколько JSON в одно сообщение.
    """
    s = (raw or "").replace("\ufeff", "").strip()
    payloads: list[dict[str, Any]] = []
    # Сначала целиком (частый случай: один объект без преамбулы; BOM уже снят)
    if s.startswith("{") and s.endswith("}"):
        direct = _parse_protocol_blob(s)
        if direct is not None:
            payloads = [direct]
    if not payloads:
        payloads = iter_parsed_protocol_payloads_any(s)
    if not payloads:
        return None
    for p in payloads:
        if p["calls"]:
            return p
    return payloads[-1]


def tool_outputs_user_message(calls: list[dict[str, Any]], outputs: list[str]) -> str:
    payload = {
        "tool_outputs": [
            {"name": calls[i]["name"], "output": outputs[i]}
            for i in range(min(len(calls), len(outputs)))
        ]
    }
    return f"{PROTOCOL_USER_PREFIX}\n{json.dumps(payload, ensure_ascii=False)}"


def extract_user_visible_assistant_text(content: str) -> str:
    """Для UI и под-агента: markdown из протокола или исходная строка.

    Если в тексте несколько JSON протокола (например два ```json), для показа пользователю берём
    последний блок без вызовов тулов с непустым markdown — а не первый (часто только tool-call).
    """
    payloads = iter_parsed_protocol_payloads_any(content)
    if not payloads:
        return content
    for p in reversed(payloads):
        if not p["calls"] and (p["assistant_markdown"] or "").strip():
            return p["assistant_markdown"]
    return payloads[-1]["assistant_markdown"]


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
                msg.pop("tool_calls", None)
                msg.pop("function_call", None)
                ch0 = dict(ch0)
                ch0["message"] = msg
                choices = [ch0] + list(choices[1:])
                out["choices"] = choices
    return out
