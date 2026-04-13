from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

from certified_turtles.agents.json_agent_protocol import (
    PROTOCOL_JSON_REPAIR_USER,
    build_protocol_system_message,
    extract_user_visible_assistant_text,
    message_text_content,
    parse_agent_response,
    parse_failure_log_preview,
    patch_completion_assistant_markdown,
    tool_outputs_user_message,
)
from certified_turtles.agents.registry import get_subagent
from certified_turtles.mws_gpt.client import MWSGPTClient
from certified_turtles.memory_runtime.request_context import RequestContext, use_request_context
from certified_turtles.agent_debug_log import agent_logger, debug_clip, summarize_messages
from certified_turtles.tools.parent_tools import get_parent_tools, parse_agent_tool_name
from certified_turtles.tools.registry import openai_tools_for_names, run_primitive_tool

logger = logging.getLogger(__name__)
_agent_log = agent_logger("loop")

# Open WebUI кладёт RAG вторым system («ответь пользователю текстом») — ломает JSON-протокол.
_SYSTEM_FORMAT_OVERRIDE = """ПРИОРИТЕТ ФОРМАТА (выше фраз «Respond to the user», «Provide a clear response», цитат [id], «Do not use XML»):
Каждый твой ответ role=assistant — ровно один JSON-объект с ключами assistant_markdown и calls. Текст или markdown вне этого JSON запрещены.
Для таблиц/файлов: скопируй реальный file_id из строки file_id="…" рядом с [CT: RAG-источник …] (это не номер цитаты [1]). Затем workspace_file_path и/или execute_python с file_id и кодом на pandas; не подставляй плейсхолдеры и не вызывай workspace_file_path внутри Python.

"""


def _json_repair_attempts_budget() -> int:
    """Сколько раз подряд при невалидном JSON добавляем [CT_PROTO_JSON_REPAIR] (0–5)."""
    try:
        n = int(os.environ.get("CT_AGENT_JSON_REPAIR_ATTEMPTS", "2"))
    except (TypeError, ValueError):
        n = 2
    return max(0, min(5, n))


def _tool_names_from_openai_tools(tool_list: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for t in tool_list:
        if t.get("type") != "function":
            continue
        fn = t.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return names


def _parent_context_body(role: str, body: str) -> str:
    """Очищает родительскую историю перед передачей под-агенту.

    Главное: не тащить целиком protocol system + каталог тулов, иначе под-агент начинает
    вызывать parent-only `agent_*` и прочие функции, которых у него нет.
    """
    if role != "system":
        return body
    marker = "--- Контекст и инструкции чата (Open WebUI / RAG) ---"
    if marker in body:
        return body.split(marker, 1)[1].strip()
    kept: list[str] = []
    for ln in body.splitlines():
        low = ln.lower()
        if "<source" in low or "[ct:" in low or "file_id=" in low:
            kept.append(ln)
    return "\n".join(kept).strip()


def _parent_dialog_snippet(messages: list[dict[str, Any]], *, max_chars: int = 8000) -> str:
    """Фрагмент истории родителя для под-агента (file_id, RAG, запрос пользователя)."""
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


def _inject_json_protocol_system(
    messages: list[dict[str, Any]],
    *,
    tool_list: list[dict[str, Any]],
) -> None:
    """Один system: протокол + каталог тулов; остальные system (RAG Open WebUI) — ниже, без второго конфликтующего system."""
    if not tool_list:
        return
    protocol = build_protocol_system_message(tool_list)
    extras: list[str] = []
    drop: list[int] = []
    for i, m in enumerate(messages):
        if m.get("role") != "system":
            continue
        txt = message_text_content(m).strip()
        if txt:
            extras.append(txt)
        drop.append(i)
    for i in reversed(drop):
        messages.pop(i)
    merged = "\n\n".join(extras)
    content = _SYSTEM_FORMAT_OVERRIDE + protocol
    if merged:
        content += "\n\n--- Контекст и инструкции чата (Open WebUI / RAG) ---\n\n" + merged
    messages.insert(0, {"role": "system", "content": content})
    _agent_log.debug(
        "inject protocol system: merged extras=%s chars, total system chars=%s",
        len(merged),
        len(content),
    )


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


def _tool_output_json_dict(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _pack_subagent_result(inner: dict[str, Any]) -> str:
    msgs = inner.get("messages") or []
    final_text = ""
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            c = m.get("content")
            if c:
                final_text = extract_user_visible_assistant_text(str(c))
                break
    if not final_text:
        final_text = "(пустой ответ под-агента)"
    parts = [f"[[под-агент]]\n{final_text}"]
    if inner.get("truncated"):
        parts.append(f"[внутренний лимит раундов: truncated={inner['truncated']}]")
    return "\n".join(parts)


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
    inner_kw = {k: v for k, v in chat_kwargs.items() if k != "parent_work"}
    inner = run_agent_chat(
        client,
        model,
        inner_messages,
        tools=inner_tools,
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


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate: ~4 chars per token across all message content."""
    total_chars = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(str(part.get("text", "")))
    return total_chars // 4


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
    """
    Цикл оркестратора: при ненулевом списке тулов — единый JSON-протокол в ответах assistant
    (см. `json_agent_protocol`) и исполнение `calls`; иначе — одиночный chat без протокола.

    Stops when cumulative tokens exceed `max_agent_tokens` or hard round ceiling (200) is hit.
    """
    _HARD_ROUND_CEILING = 200
    with use_request_context(request_context or RequestContext(session_id="global", scope_id="global")):
        work = copy.deepcopy(messages)
        tool_list = get_parent_tools() if tools is None else tools
        use_json_protocol = bool(tool_list)
        _agent_log.debug(
            "run_agent_chat start model=%s depth=%s max_agent_tokens=%s json_proto=%s tools=%s",
            model,
            delegate_depth,
            max_agent_tokens,
            use_json_protocol,
            len(tool_list or []),
        )
        _agent_log.debug("messages before inject:\n%s", summarize_messages(work))
        if use_json_protocol:
            _inject_json_protocol_system(work, tool_list=tool_list)
            _agent_log.debug("messages after inject:\n%s", summarize_messages(work))
        allowed = _tool_names_from_openai_tools(tool_list) if tool_list else set()
        last_raw: dict[str, Any] | None = None
        rounds = 0
        tokens_used = 0
        json_repair_attempts = _json_repair_attempts_budget()

        while tokens_used < max_agent_tokens and rounds < _HARD_ROUND_CEILING:
            rounds += 1
            call_kwargs = {k: v for k, v in chat_kwargs.items() if k not in ("tools", "tool_choice", "request_context", "max_tool_rounds")}
            if not use_json_protocol:
                if tools is not None and tool_choice is not None:
                    call_kwargs["tool_choice"] = tool_choice
                if tools is not None:
                    call_kwargs["tools"] = tools
            _agent_log.debug(
                "--- round %s tokens=%s/%s messages_in_flight=%s call_kwargs_keys=%s",
                rounds,
                tokens_used,
                max_agent_tokens,
                len(work),
                sorted(call_kwargs.keys()),
            )
            _agent_log.debug("round %s request history:\n%s", rounds, summarize_messages(work, preview=300))
            last_raw = client.chat_completions(model, work, **call_kwargs)
            # Track token usage
            usage = last_raw.get("usage") if isinstance(last_raw, dict) else None
            if isinstance(usage, dict) and "total_tokens" in usage:
                tokens_used += int(usage["total_tokens"])
            else:
                tokens_used += _estimate_tokens(work[-1:])  # fallback estimate
            _agent_log.debug("round %s tokens_used=%s / %s", rounds, tokens_used, max_agent_tokens)
            choice = _first_choice(last_raw)
            msg = _choice_message(choice)
            raw_text = message_text_content(msg)
            _agent_log.debug(
                "round %s raw assistant len=%s body:\n%s",
                rounds,
                len(raw_text),
                debug_clip(raw_text),
            )

            if not use_json_protocol:
                work.append(copy.deepcopy(msg))
                _agent_log.debug(
                    "plain chat exit round=%s assistant_preview=\n%s",
                    rounds,
                    debug_clip(raw_text),
                )
                return {
                    "messages": work,
                    "completion": last_raw,
                    "tool_rounds_used": rounds,
                    "truncated": False,
                }

            parsed = parse_agent_response(raw_text)
            if parsed is None:
                _agent_log.debug("round %s parse_agent_response -> None (не JSON протокол)", rounds)
                if use_json_protocol and json_repair_attempts > 0:
                    json_repair_attempts -= 1
                    logger.warning(
                        "agent JSON protocol: parse failed at round %s, repair prompt "
                        "(дальнейших ремонтов JSON осталось: %s). assistant_raw_preview:\n%s",
                        rounds,
                        json_repair_attempts,
                        parse_failure_log_preview(raw_text),
                    )
                    _agent_log.debug("repair prompt appended, осталось попыток ремонта: %s", json_repair_attempts)
                    work.append({"role": "assistant", "content": raw_text})
                    work.append({"role": "user", "content": PROTOCOL_JSON_REPAIR_USER})
                    continue
                logger.warning(
                    "agent JSON protocol: parse failed at round %s — выходим из протокола, "
                    "пользователю уйдёт сырой/видимый текст без тулов. assistant_raw_preview:\n%s",
                    rounds,
                    parse_failure_log_preview(raw_text),
                )
                work.append({"role": "assistant", "content": raw_text})
                visible = extract_user_visible_assistant_text(raw_text)
                patched = patch_completion_assistant_markdown(last_raw, visible)
                _agent_log.debug(
                    "parse failed final visible_preview=\n%s",
                    debug_clip(visible),
                )
                return {
                    "messages": work,
                    "completion": patched,
                    "tool_rounds_used": rounds,
                    "truncated": False,
                }

            work.append({"role": "assistant", "content": raw_text})
            calls = parsed["calls"]
            _agent_log.debug(
                "round %s parsed ok calls=%s assistant_markdown_len=%s",
                rounds,
                json.dumps([c.get("name") for c in calls], ensure_ascii=False),
                len(parsed.get("assistant_markdown") or ""),
            )
            if calls:
                _agent_log.debug(
                    "round %s call details: %s",
                    rounds,
                    debug_clip(json.dumps(calls, ensure_ascii=False, indent=2)),
                )
            if not calls:
                md = parsed["assistant_markdown"]
                _agent_log.debug("round %s финал без тулов, markdown:\n%s", rounds, debug_clip(md))
                patched = patch_completion_assistant_markdown(last_raw, md)
                _agent_log.debug(
                    "run_agent_chat final assistant_markdown rounds=%s out=\n%s",
                    rounds,
                    debug_clip(md),
                )
                return {
                    "messages": work,
                    "completion": patched,
                    "tool_rounds_used": rounds,
                    "truncated": False,
                }

            outputs: list[str] = []
            bound_file_id: str | None = None
            for c in calls:
                name = c["name"]
                args = copy.deepcopy(c["arguments"])
                if (
                    name == "execute_python"
                    and isinstance(args, dict)
                    and bound_file_id
                    and not str(args.get("file_id") or "").strip()
                ):
                    args["file_id"] = bound_file_id
                    _agent_log.debug(
                        "round %s auto-bind execute_python.file_id=%s from prior workspace_file_path",
                        rounds,
                        bound_file_id,
                    )
                _agent_log.debug(
                    "round %s invoke tool name=%s args=%s",
                    rounds,
                    name,
                    debug_clip(json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)),
                )
                if name not in allowed:
                    outputs.append(
                        json.dumps({"error": "unknown_tool", "name": name}, ensure_ascii=False),
                    )
                    continue
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
                if name == "workspace_file_path":
                    data = _tool_output_json_dict(out)
                    maybe_fid = data.get("file_id") if isinstance(data, dict) else None
                    if isinstance(maybe_fid, str) and maybe_fid.strip():
                        bound_file_id = maybe_fid.strip()
                outputs.append(out)
            tool_msg = tool_outputs_user_message(calls, outputs)
            _agent_log.debug("round %s tool_outputs user msg len=%s", rounds, len(tool_msg))
            work.append({"role": "user", "content": tool_msg})

        visible = ""
        if last_raw is not None:
            try:
                lm = _choice_message(_first_choice(last_raw))
                visible = extract_user_visible_assistant_text(message_text_content(lm))
            except ValueError:
                visible = ""
        tail = "\n\n[лимит токенов агента: ответ может быть неполным.]" if not visible.strip() else ""
        patched = patch_completion_assistant_markdown(last_raw or {}, (visible or "") + tail)
        _agent_log.debug(
            "run_agent_chat TRUNCATED rounds_used=%s visible_preview=\n%s",
            rounds,
            debug_clip((visible or "") + tail),
        )
        return {
            "messages": work,
            "completion": patched,
            "tool_rounds_used": rounds,
            "truncated": True,
        }
