from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Iterator

from certified_turtles.agent_debug_log import agent_logger, debug_clip, summarize_messages
from certified_turtles.agents.execute_python_intent import llm_should_skip_execute_python
from certified_turtles.agents.json_agent_protocol import message_text_content, parse_agent_response
from certified_turtles.agents.registry import DEEP_RESEARCH_AGENT_ID, get_subagent
from certified_turtles.mws_gpt.client import MWSGPTClient
from certified_turtles.prompts import load_prompt
from certified_turtles.tools.parent_tools import get_parent_tools, parse_agent_tool_name
from certified_turtles.tools.registry import openai_tools_for_names, run_primitive_tool

logger = logging.getLogger(__name__)
_agent_log = agent_logger("loop")

_AGENT_STREAMING_SYSTEM = load_prompt("agent_streaming_system.md").strip()


def _last_chat_user_plain_text(work: list[dict[str, Any]]) -> str:
    """Последнее обычное user-сообщение чата без tool/result-шума."""
    for m in reversed(work):
        if m.get("role") != "user":
            continue
        t = message_text_content(m).strip()
        if t:
            return t
    return ""




def _tool_names_from_openai_tools(tool_list: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for t in tool_list:
        if t.get("type") != "function":
            continue
        fn = t.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return names


_OWEBUI_TOOL_ROUTER_PARA = re.compile(
    r"(?ms)^\s*Available Tools:\s*\[[^\]]*\]\s*"
    r".*?(?=\n\s*\n|\Z)",
)


def _strip_openwebui_tool_router_noise(text: str) -> str:
    """Убирает noise-блоки Open WebUI перед передачей контекста под-агенту."""
    s = text.strip()
    if not s:
        return s
    s = _OWEBUI_TOOL_ROUTER_PARA.sub("", s)
    parts = re.split(r"\n\s*\n+", s)
    kept: list[str] = []
    for p in parts:
        pl = p.lstrip()
        low = pl.lower()
        if low.startswith("available tools:"):
            continue
        if "choose and return the correct tool" in low and "available tools" in low:
            continue
        if "return only the json object" in low and "tool_calls" in low:
            continue
        kept.append(p.strip())
    return "\n\n".join(x for x in kept if x).strip()


def _parent_context_body(role: str, body: str) -> str:
    """Очищает историю родителя, чтобы под-агент не видел служебный шум основного чата."""
    if role != "system":
        return body
    marker = "--- Контекст и инструкции чата (Open WebUI / RAG) ---"
    if marker in body:
        tail = body.split(marker, 1)[1].strip()
        return _strip_openwebui_tool_router_noise(tail)
    kept: list[str] = []
    for ln in body.splitlines():
        low = ln.lower()
        if "<source" in low or "[ct:" in low or "file_id=" in low:
            kept.append(ln)
    return "\n".join(kept).strip()


def _parent_dialog_snippet(messages: list[dict[str, Any]], *, max_chars: int = 8000) -> str:
    """Короткий фрагмент истории родителя для под-агента."""
    parts: list[str] = []
    tail = messages[-8:] if len(messages) > 8 else messages
    for m in tail:
        role = m.get("role")
        if role not in ("user", "assistant", "system"):
            continue
        body = _parent_context_body(role, message_text_content(m))
        if not body.strip():
            continue
        if role == "system" and len(body) > 14_000:
            body = body[-4500:]
        parts.append(f"<<{role}>>\n{body}")
    s = "\n\n".join(parts)
    if len(s) > max_chars:
        return "…\n" + s[-max_chars:]
    return s


def _forced_subagent_messages(
    agent_id: str,
    parent_messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    spec = get_subagent(agent_id)
    if spec is None:
        raise ValueError(f"Неизвестный forced_agent_id: {agent_id}")

    task = _last_chat_user_plain_text(parent_messages).strip()
    if not task:
        task = "Выполни задачу по текущему контексту диалога."

    user_parts = [task]
    context = _parent_dialog_snippet(parent_messages)
    if context.strip():
        user_parts.append(f"\n\nКонтекст с основного диалога:\n{context}")

    inner_messages: list[dict[str, Any]] = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]
    return inner_messages, spec.tool_names


def _inject_agent_system(messages: list[dict[str, Any]], *, tool_list: list[dict[str, Any]]) -> None:
    if not tool_list:
        return
    names = _tool_names_from_openai_tools(tool_list)
    parts = [_AGENT_STREAMING_SYSTEM]
    if "workspace_file_path" in names:
        parts.append(
            "Open WebUI / RAG: в тексте могут быть теги <source id=\"…\" name=\"…\">. "
            "Их id — это номер цитаты, а не file_id для workspace_file_path. "
            "Бери только file_id из строк с префиксом [CT: RAG-источник ...]."
        )
    if any(n.startswith("google_docs_") for n in names):
        from certified_turtles.tools.builtins.google_docs import agent_system_prompt_google_docs_section

        parts.append(agent_system_prompt_google_docs_section())
    messages.insert(0, {"role": "system", "content": "\n\n".join(parts)})
    _agent_log.debug("messages after agent system inject:\n%s", summarize_messages(messages))


def _agent_stream_chat_enabled() -> bool:
    v = os.environ.get("CT_AGENT_STREAM_CHAT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _merge_chat_completion_stream_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Собирает нестриминговый ответ из SSE-чанков OpenAI-совместимого chat/completions stream."""
    role = "assistant"
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    for chunk in chunks:
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        c0 = choices[0]
        if not isinstance(c0, dict):
            continue
        delta = c0.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        if isinstance(delta.get("role"), str) and delta["role"].strip():
            role = delta["role"]
        c = delta.get("content")
        if isinstance(c, str) and c:
            content_parts.append(c)
        for rk in ("reasoning", "reasoning_content"):
            piece = delta.get(rk)
            if isinstance(piece, str) and piece:
                reasoning_parts.append(piece)
        tcs = delta.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get("index", 0))
                if idx not in tool_calls:
                    tool_calls[idx] = {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                if tc.get("id"):
                    tool_calls[idx]["id"] = tc["id"]
                if tc.get("type"):
                    tool_calls[idx]["type"] = tc["type"]
                fn = tc.get("function") or {}
                if isinstance(fn, dict):
                    if fn.get("name"):
                        tool_calls[idx]["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        prev = tool_calls[idx]["function"].get("arguments") or ""
                        if isinstance(prev, str):
                            tool_calls[idx]["function"]["arguments"] = prev + str(fn["arguments"])
        fr = c0.get("finish_reason")
        if fr:
            finish_reason = fr
    msg: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
    rs = "".join(reasoning_parts)
    if rs:
        msg["reasoning"] = rs
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {"choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}]}


def _first_choice(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Ответ chat/completions без choices")
    ch0 = choices[0]
    if not isinstance(ch0, dict):
        raise ValueError("choices[0] не объект")
    return ch0


def _choice_message(choice: dict[str, Any]) -> dict[str, Any]:
    msg = choice.get("message")
    if not isinstance(msg, dict):
        raise ValueError("В ответе нет message")
    return msg


def _parse_tool_output_json_dict(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw_arguments": raw[:4000]}
    return data if isinstance(data, dict) else {}


def _tool_call_records_from_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw_calls = msg.get("tool_calls")
    if not isinstance(raw_calls, list):
        return out
    for idx, call in enumerate(raw_calls):
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_{idx}_{uuid.uuid4().hex[:8]}"
        args = _parse_tool_arguments(fn.get("arguments"))
        out.append(
            {
                "id": call_id,
                "name": name.strip(),
                "arguments": args,
                "history": {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name.strip(),
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                },
            }
        )
    return out


def _legacy_tool_call_records(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, call in enumerate(calls):
        name = call.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        args = call.get("arguments")
        if not isinstance(args, dict):
            args = {}
        call_id = f"legacy_call_{idx}_{uuid.uuid4().hex[:8]}"
        out.append(
            {
                "id": call_id,
                "name": name.strip(),
                "arguments": copy.deepcopy(args),
                "history": {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name.strip(),
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                },
            }
        )
    return out


def _extract_assistant_turn(msg: dict[str, Any], *, use_tools: bool) -> tuple[str, list[dict[str, Any]]]:
    assistant_text = message_text_content(msg).strip()
    tool_calls = _tool_call_records_from_message(msg)
    if use_tools and assistant_text:
        parsed = parse_agent_response(assistant_text)
        if parsed is not None:
            assistant_text = (parsed.get("assistant_markdown") or "").strip()
            if not tool_calls:
                tool_calls = _legacy_tool_call_records(parsed.get("calls") or [])
    return assistant_text, tool_calls


def _assistant_message_for_history(
    msg: dict[str, Any],
    *,
    assistant_text: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    history_msg = {
        "role": "assistant",
        "content": assistant_text,
    }
    if tool_calls:
        history_msg["tool_calls"] = [copy.deepcopy(call["history"]) for call in tool_calls]
    elif isinstance(msg.get("tool_calls"), list):
        history_msg["tool_calls"] = copy.deepcopy(msg["tool_calls"])
    return history_msg


def _tool_status_text(name: str, arguments: dict[str, Any]) -> str:
    agent_id = parse_agent_tool_name(name)
    if agent_id is not None:
        return f"Запускаю под-агента `{agent_id}`."
    mapping = {
        "web_search": "Ищу информацию в сети.",
        "fetch_url": "Открываю и читаю страницу.",
        "workspace_file_path": "Проверяю путь к загруженному файлу.",
        "read_workspace_file": "Читаю файл из рабочей области.",
        "execute_python": "Запускаю Python-код и проверяю результат.",
        "generate_presentation": "Создаю презентацию.",
        "generate_image": "Генерирую изображение.",
        "transcribe_workspace_audio": "Расшифровываю аудио.",
        "google_docs_get_document": "Читаю документ из Google Docs.",
        "google_docs_export_document": "Экспортирую документ из Google Docs.",
    }
    if name in mapping:
        return mapping[name]
    return f"Выполняю инструмент `{name}`."


def _reasoning_text_for_tool_call(name: str, arguments: dict[str, Any]) -> str:
    agent_id = parse_agent_tool_name(name)
    if agent_id == "deep_research":
        task = str(arguments.get("task") or "").strip()
        if task:
            return f"Запускаю GPT Researcher по теме «{task}» (отдельный venv, см. github.com/assafelovic/gpt-researcher)."
        return "Запускаю GPT Researcher (github.com/assafelovic/gpt-researcher) для полного отчёта."
    if agent_id == "research":
        task = str(arguments.get("task") or "").strip()
        if task:
            return f"Сначала быстро соберу проверяемые источники по теме «{task}», затем сведу выводы и ссылки."
        return "Сначала соберу несколько проверяемых источников и затем сведу выводы."
    if agent_id is not None:
        return f"Сначала делегирую специализированный шаг под-агенту `{agent_id}`, затем встрою результат в общий ответ."

    if name == "web_search":
        query = str(arguments.get("query") or "").strip()
        if query:
            return f"Сначала найду по запросу «{query}» несколько релевантных источников, потом открою самые сильные из них."
        return "Сначала найду релевантные источники, потом открою самые полезные."
    if name == "fetch_url":
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"Теперь открою источник {url}, чтобы опереться на текст страницы, а не только на выдачу поиска."
        return "Теперь открою источник и проверю содержание страницы."
    if name == "workspace_file_path":
        return "Сначала получу путь к загруженному файлу, чтобы дальше корректно работать с ним инструментами."
    if name == "read_workspace_file":
        return "Сначала прочитаю содержимое файла, чтобы понять структуру данных и следующие шаги."
    if name == "execute_python":
        return "Теперь прогоню код и проверю результат вычислений, а не буду угадывать ответ."
    if name == "generate_presentation":
        return "Сначала соберу структуру и затем сгенерирую полноценную презентацию."
    if name == "generate_image":
        return "Сначала подготовлю описание и затем сгенерирую изображение."
    return ""


def _reasoning_text_for_turn(assistant_text: str, tool_calls: list[dict[str, Any]]) -> str:
    if assistant_text:
        return assistant_text
    if not tool_calls:
        return ""
    if len(tool_calls) == 1:
        custom = _reasoning_text_for_tool_call(tool_calls[0]["name"], tool_calls[0]["arguments"])
        if custom:
            return custom
    names = [f"`{call['name']}`" for call in tool_calls]
    return "Сначала выполню несколько шагов через инструменты: " + ", ".join(names) + "."


def _push_trace_event(trace: list[dict[str, Any]], kind: str, text: str, **meta: Any) -> dict[str, Any] | None:
    cleaned = text.strip("\n")
    if not cleaned.strip():
        return None
    item = {"type": kind, "text": cleaned, **meta}
    trace.append(copy.deepcopy(item))
    return item


def _render_visible_markdown(trace: list[dict[str, Any]]) -> str:
    reasoning_parts: list[str] = []
    final_parts: list[str] = []
    for item in trace:
        kind = item.get("type")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if kind == "reasoning":
            reasoning_parts.append(text)
        elif kind == "status":
            reasoning_parts.append(f"- {text}")
        elif kind == "final":
            final_parts.append(text)
    parts: list[str] = []
    if reasoning_parts:
        parts.append("### Размышление\n\n" + "\n\n".join(reasoning_parts))
    final_text = "".join(final_parts).strip()
    if final_text:
        if parts:
            parts.append("### Ответ\n\n" + final_text)
        else:
            parts.append(final_text)
    return "\n\n".join(parts).strip()


def _completion_with_visible_markdown(
    raw: dict[str, Any] | None,
    *,
    model: str,
    visible_text: str,
    trace: list[dict[str, Any]],
    finish_reason: str = "stop",
) -> dict[str, Any]:
    if isinstance(raw, dict):
        completion = copy.deepcopy(raw)
    else:
        completion = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": visible_text}, "finish_reason": finish_reason}],
        }
    choices = completion.get("choices")
    if isinstance(choices, list) and choices:
        ch0 = choices[0]
        if isinstance(ch0, dict):
            msg = ch0.get("message")
            if not isinstance(msg, dict):
                msg = {}
                ch0["message"] = msg
            msg["role"] = "assistant"
            msg["content"] = visible_text
            msg.pop("tool_calls", None)
            msg.pop("function_call", None)
            ch0["finish_reason"] = finish_reason
    completion["output"] = copy.deepcopy(trace)
    return completion


def _pack_subagent_result(inner: dict[str, Any]) -> str:
    final_text = str(inner.get("assistant_final") or inner.get("assistant_visible") or "").strip()
    if not final_text:
        final_text = "(пустой ответ под-агента)"
    parts = [f"[[под-агент]]\n{final_text}"]
    if inner.get("truncated"):
        parts.append(f"[внутренний лимит раундов: truncated={inner['truncated']}]")
    return "\n".join(parts)


def _unwrap_subagent_result(raw: str) -> str:
    s = raw.strip()
    prefix = "[[под-агент]]"
    if s.startswith(prefix):
        s = s[len(prefix) :].lstrip()
    return s.strip()


def _invoke_subagent(
    client: MWSGPTClient,
    model: str,
    agent_id: str,
    arguments: dict[str, Any],
    *,
    delegate_depth: int,
    max_delegate_depth: int,
    parent_work: list[dict[str, Any]] | None = None,
    **chat_kwargs: Any,
) -> str:
    spec = get_subagent(agent_id)
    if spec is None:
        return json.dumps({"error": f"Неизвестный под-агент: {agent_id}"}, ensure_ascii=False)
    if delegate_depth >= max_delegate_depth:
        return json.dumps(
            {"error": "nested_delegate_limit", "detail": f"max_delegate_depth={max_delegate_depth}"},
            ensure_ascii=False,
        )
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        return json.dumps({"error": "Нужен непустой параметр task."}, ensure_ascii=False)
    ctx = arguments.get("context")
    user_parts = [task.strip()]
    if isinstance(ctx, str) and ctx.strip():
        user_parts.append(f"\n\nКонтекст с основного диалога:\n{ctx.strip()}")
    elif parent_work:
        user_parts.append(f"\n\nКонтекст с основного диалога:\n{_parent_dialog_snippet(parent_work)}")
    if agent_id == DEEP_RESEARCH_AGENT_ID:
        from certified_turtles.integrations.gpt_researcher_runner import run_gpt_researcher_sync_with_meta

        query = "\n".join(user_parts)
        meta = run_gpt_researcher_sync_with_meta(query)
        if meta.get("ok"):
            report = str(meta.get("report") or "")
            return _pack_subagent_result(
                {
                    "assistant_final": report,
                    "assistant_visible": report,
                    "truncated": False,
                    "tool_rounds_used": 1,
                }
            )
        return json.dumps(
            {"error": "gpt_researcher_failed", "detail": str(meta.get("error") or "unknown")},
            ensure_ascii=False,
        )
    inner_messages: list[dict[str, Any]] = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]
    inner_tools = openai_tools_for_names(spec.tool_names)
    _agent_log.debug(
        "subagent start id=%s depth=%s task_preview=%s",
        agent_id,
        delegate_depth,
        json.dumps(task.strip()[:240], ensure_ascii=False),
    )
    inner_kw = {k: v for k, v in chat_kwargs.items() if k not in ("parent_work",)}
    inner = run_agent_chat(
        client,
        model,
        inner_messages,
        tools=inner_tools,
        tool_choice="required" if inner_tools else "auto",
        max_tool_rounds=spec.max_inner_rounds,
        delegate_depth=delegate_depth + 1,
        max_delegate_depth=max_delegate_depth,
        **inner_kw,
    )
    _agent_log.debug(
        "subagent end id=%s truncated=%s inner_rounds=%s",
        agent_id,
        inner.get("truncated"),
        inner.get("tool_rounds_used"),
    )
    return _pack_subagent_result(inner)


def _execute_tool_call(
    name: str,
    arguments: dict[str, Any],
    *,
    client: MWSGPTClient,
    model: str,
    delegate_depth: int,
    max_delegate_depth: int,
    parent_work: list[dict[str, Any]] | None = None,
    **chat_kwargs: Any,
) -> str:
    agent_id = parse_agent_tool_name(name)
    if agent_id is not None:
        return _invoke_subagent(
            client,
            model,
            agent_id,
            arguments,
            delegate_depth=delegate_depth,
            max_delegate_depth=max_delegate_depth,
            parent_work=parent_work,
            **chat_kwargs,
        )
    return run_primitive_tool(name, arguments)


def _stream_deep_research_gpt_researcher(
    client: MWSGPTClient,
    model: str,
    messages: list[dict[str, Any]],
    *,
    delegate_depth: int,
    max_tool_rounds: int,
    max_delegate_depth: int,
    **chat_kwargs: Any,
) -> Iterator[dict[str, Any]]:
    """Режим ct_mode=deep_research: отчёт целиком через GPT Researcher (subprocess), без внутреннего LLM-цикла."""
    del client, max_tool_rounds, max_delegate_depth, chat_kwargs
    inner_messages, _ = _forced_subagent_messages(DEEP_RESEARCH_AGENT_ID, messages)
    parts: list[str] = []
    for m in inner_messages:
        if m.get("role") not in ("system", "user"):
            continue
        t = message_text_content(m).strip()
        if t:
            parts.append(t)
    query = "\n\n".join(parts).strip()
    trace: list[dict[str, Any]] = []
    st = _push_trace_event(
        trace,
        "status",
        "Запуск GPT Researcher (github.com/assafelovic/gpt-researcher) в отдельном venv…",
        depth=delegate_depth,
    )
    if st is not None:
        yield st
    from certified_turtles.integrations.gpt_researcher_runner import run_gpt_researcher_sync_with_meta

    meta = run_gpt_researcher_sync_with_meta(query)
    if meta.get("ok"):
        text = str(meta.get("report") or "")
        fe = _push_trace_event(trace, "final", text, round=1, depth=delegate_depth)
        if fe is not None:
            yield fe
        work = copy.deepcopy(inner_messages)
        work.append({"role": "assistant", "content": text})
        visible = _render_visible_markdown(trace)
        result = {
            "messages": work,
            "completion": _completion_with_visible_markdown(None, model=model, visible_text=visible, trace=trace),
            "tool_rounds_used": 1,
            "truncated": False,
            "assistant_visible": visible,
            "assistant_final": text,
            "output": copy.deepcopy(trace),
        }
        yield {"type": "done", "result": result}
        return
    err = str(meta.get("error") or "unknown")
    err_ev = _push_trace_event(trace, "final", f"[Ошибка GPT Researcher] {err}", round=1, depth=delegate_depth)
    if err_ev is not None:
        yield err_ev
    visible = _render_visible_markdown(trace)
    fail_text = f"[Ошибка GPT Researcher] {err}"
    result = {
        "messages": copy.deepcopy(inner_messages)
        + [{"role": "assistant", "content": fail_text}],
        "completion": _completion_with_visible_markdown(None, model=model, visible_text=visible, trace=trace),
        "tool_rounds_used": 1,
        "truncated": False,
        "assistant_visible": visible,
        "assistant_final": fail_text,
        "output": copy.deepcopy(trace),
    }
    yield {"type": "done", "result": result}


def stream_agent_chat(
    client: MWSGPTClient,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = "auto",
    max_tool_rounds: int = 10,
    delegate_depth: int = 0,
    max_delegate_depth: int = 3,
    forced_agent_id: str | None = None,
    **chat_kwargs: Any,
) -> Iterator[dict[str, Any]]:
    """Agent-first runtime с публичными reasoning/status событиями и финальным ответом."""
    if forced_agent_id:
        if forced_agent_id == DEEP_RESEARCH_AGENT_ID:
            yield from _stream_deep_research_gpt_researcher(
                client,
                model,
                messages,
                delegate_depth=delegate_depth,
                max_tool_rounds=max_tool_rounds,
                max_delegate_depth=max_delegate_depth,
                **chat_kwargs,
            )
            return
        inner_messages, tool_names = _forced_subagent_messages(forced_agent_id, messages)
        inner_tools = openai_tools_for_names(tool_names)
        effective_rounds = max(max_tool_rounds, get_subagent(forced_agent_id).max_inner_rounds)
        # "required" на первом раунде — модель обязана вызвать тулы, а не
        # галлюцинировать отчёт. Внутри цикла после первого раунда
        # переключится на "auto" для финального ответа.
        first_tool_choice = "required" if inner_tools else "auto"
        yield from stream_agent_chat(
            client,
            model,
            inner_messages,
            tools=inner_tools,
            tool_choice=first_tool_choice,
            max_tool_rounds=effective_rounds,
            delegate_depth=delegate_depth,
            max_delegate_depth=max_delegate_depth,
            **chat_kwargs,
        )
        return

    work = copy.deepcopy(messages)
    tool_list = get_parent_tools() if tools is None else tools
    use_tools = bool(tool_list)
    _agent_log.debug(
        "stream_agent_chat start model=%s depth=%s max_rounds=%s tools=%s",
        model,
        delegate_depth,
        max_tool_rounds,
        len(tool_list or []),
    )
    _agent_log.debug("messages before inject:\n%s", summarize_messages(work))
    if use_tools:
        _inject_agent_system(work, tool_list=tool_list)
    allowed = _tool_names_from_openai_tools(tool_list) if tool_list else set()
    trace: list[dict[str, Any]] = []
    last_raw: dict[str, Any] | None = None
    rounds = 0
    # Для первого раунда: если tool_choice="required", заставляем модель
    # использовать инструменты, а со второго раунда переключаемся на "auto".
    effective_tool_choice = tool_choice

    while rounds < max_tool_rounds:
        rounds += 1
        call_kwargs = {k: v for k, v in chat_kwargs.items() if k not in ("tools", "tool_choice")}
        if use_tools:
            call_kwargs["tools"] = tool_list
            if effective_tool_choice is not None:
                call_kwargs["tool_choice"] = effective_tool_choice
        _agent_log.debug(
            "--- round %s/%s messages_in_flight=%s call_kwargs_keys=%s",
            rounds,
            max_tool_rounds,
            len(work),
            sorted(call_kwargs.keys()),
        )
        _agent_log.debug("round %s request history:\n%s", rounds, summarize_messages(work, preview=300))
        stream_chunks: list[dict[str, Any]] = []
        stream_fn = getattr(client, "chat_completions_stream", None)
        if _agent_stream_chat_enabled() and callable(stream_fn):
            try:
                for chunk in stream_fn(model, work, **call_kwargs):
                    stream_chunks.append(chunk)
                    choice0 = (chunk.get("choices") or [{}])[0]
                    if not isinstance(choice0, dict):
                        continue
                    delta = choice0.get("delta")
                    if not isinstance(delta, dict):
                        delta = {}
                    for rk in ("reasoning", "reasoning_content"):
                        piece = delta.get(rk)
                        if isinstance(piece, str) and piece:
                            yield {"type": "reasoning_stream", "text": piece}
                    c = delta.get("content")
                    if isinstance(c, str) and c:
                        if effective_tool_choice == "required":
                            yield {"type": "reasoning_stream", "text": c}
                        else:
                            yield {"type": "content_stream", "text": c}
                if not stream_chunks:
                    raise ValueError("пустой SSE stream от chat/completions")
                last_raw = _merge_chat_completion_stream_chunks(stream_chunks)
            except Exception as e:
                _agent_log.warning("chat_completions_stream не удался, fallback на обычный вызов: %s", e)
                last_raw = client.chat_completions(model, work, **call_kwargs)
        else:
            last_raw = client.chat_completions(model, work, **call_kwargs)
        choice = _first_choice(last_raw)
        msg = _choice_message(choice)
        assistant_text, tool_calls = _extract_assistant_turn(msg, use_tools=use_tools)
        _agent_log.debug(
            "round %s assistant_text len=%s tool_calls=%s",
            rounds,
            len(assistant_text),
            debug_clip(json.dumps([c["name"] for c in tool_calls], ensure_ascii=False)),
        )
        work.append(
            _assistant_message_for_history(
                msg,
                assistant_text=assistant_text,
                tool_calls=tool_calls,
            )
        )
        reasoning_text = _reasoning_text_for_turn(assistant_text, tool_calls)
        # В финальном раунде без tool_calls весь assistant_text — это уже ответ пользователю.
        # Отдельное событие reasoning совпало бы с final и дублировало текст в стриминге и в markdown.
        skip_reasoning_as_duplicate_of_final = (not tool_calls) and bool(str(assistant_text).strip())
        if not skip_reasoning_as_duplicate_of_final:
            reasoning_event = _push_trace_event(
                trace,
                "reasoning",
                reasoning_text,
                round=rounds,
                depth=delegate_depth,
            )
            if reasoning_event is not None:
                yield reasoning_event

        if not use_tools or not tool_calls:
            final_event = _push_trace_event(trace, "final", assistant_text, round=rounds, depth=delegate_depth)
            if final_event is not None:
                yield final_event
            visible = _render_visible_markdown(trace)
            result = {
                "messages": work,
                "completion": _completion_with_visible_markdown(
                    last_raw,
                    model=model,
                    visible_text=visible,
                    trace=trace,
                ),
                "tool_rounds_used": rounds,
                "truncated": False,
                "assistant_visible": visible,
                "assistant_final": assistant_text,
                "output": copy.deepcopy(trace),
            }
            yield {"type": "done", "result": result}
            return

        bound_file_id: str | None = None
        single_subagent_passthrough: str | None = None
        for call in tool_calls:
            name = call["name"]
            args = copy.deepcopy(call["arguments"])
            if (
                name == "execute_python"
                and isinstance(args, dict)
                and bound_file_id
                and not str(args.get("file_id") or "").strip()
            ):
                args["file_id"] = bound_file_id
            status_event = _push_trace_event(
                trace,
                "status",
                _tool_status_text(name, args),
                tool_name=name,
                round=rounds,
                depth=delegate_depth,
            )
            if status_event is not None:
                yield status_event
            _agent_log.debug(
                "round %s invoke tool name=%s args=%s",
                rounds,
                name,
                debug_clip(json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)),
            )
            if name not in allowed:
                out = json.dumps({"error": "unknown_tool", "name": name}, ensure_ascii=False)
            elif name == "execute_python" and llm_should_skip_execute_python(
                client,
                model,
                _last_chat_user_plain_text(work),
            ):
                out = json.dumps(
                    {
                        "error": "execute_python_skipped",
                        "detail": (
                            "Запрос выглядит как выдача кода без запуска. "
                            "Помести код в обычный текст ответа и не вызывай execute_python."
                        ),
                    },
                    ensure_ascii=False,
                )
            else:
                out = _execute_tool_call(
                    name,
                    args,
                    client=client,
                    model=model,
                    delegate_depth=delegate_depth,
                    max_delegate_depth=max_delegate_depth,
                    parent_work=work,
                    **chat_kwargs,
                )
            _agent_log.debug(
                "round %s tool result name=%s out_len=%s out_preview=\n%s",
                rounds,
                name,
                len(out),
                debug_clip(out),
            )
            agent_id = parse_agent_tool_name(name)
            if (
                len(tool_calls) == 1
                and agent_id in {"research", "deep_research"}
                and out.strip().startswith("[[под-агент]]")
            ):
                single_subagent_passthrough = _unwrap_subagent_result(out)
            if name == "workspace_file_path":
                data = _parse_tool_output_json_dict(out)
                maybe_fid = data.get("file_id") if isinstance(data, dict) else None
                if isinstance(maybe_fid, str) and maybe_fid.strip():
                    bound_file_id = maybe_fid.strip()
            work.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": name,
                    "content": out,
                }
            )
        # После первого раунда с "required" переключаемся на "auto",
        # чтобы модель могла написать финальный ответ без тулов.
        if effective_tool_choice == "required":
            effective_tool_choice = "auto"
        if single_subagent_passthrough:
            final_event = _push_trace_event(
                trace,
                "final",
                single_subagent_passthrough,
                round=rounds,
                depth=delegate_depth,
            )
            if final_event is not None:
                yield final_event
            visible = _render_visible_markdown(trace)
            result = {
                "messages": work,
                "completion": _completion_with_visible_markdown(
                    last_raw,
                    model=model,
                    visible_text=visible,
                    trace=trace,
                ),
                "tool_rounds_used": rounds,
                "truncated": False,
                "assistant_visible": visible,
                "assistant_final": single_subagent_passthrough,
                "output": copy.deepcopy(trace),
            }
            yield {"type": "done", "result": result}
            return

    tail = "Достиг лимита агентных раундов, поэтому ответ может быть неполным."
    tail_event = _push_trace_event(trace, "status", tail, depth=delegate_depth)
    if tail_event is not None:
        yield tail_event
    visible = _render_visible_markdown(trace)
    result = {
        "messages": work,
        "completion": _completion_with_visible_markdown(
            last_raw,
            model=model,
            visible_text=visible,
            trace=trace,
            finish_reason="length",
        ),
        "tool_rounds_used": rounds,
        "truncated": True,
        "assistant_visible": visible,
        "assistant_final": "",
        "output": copy.deepcopy(trace),
    }
    yield {"type": "done", "result": result}


def run_agent_chat(
    client: MWSGPTClient,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = "auto",
    max_agent_tokens: int = 128_000,
    delegate_depth: int = 0,
    max_delegate_depth: int = 3,
    request_context: RequestContext | None = None,
    **chat_kwargs: Any,
) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    for event in stream_agent_chat(
        client,
        model,
        messages,
        tools=tools,
        tool_choice=tool_choice,
        max_tool_rounds=max_tool_rounds,
        delegate_depth=delegate_depth,
        max_delegate_depth=max_delegate_depth,
        **chat_kwargs,
    ):
        if event.get("type") == "done":
            result = event.get("result")
            break
    if result is None:
        raise RuntimeError("agent runtime завершился без финального результата")
    return result
