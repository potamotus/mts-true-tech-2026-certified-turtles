from __future__ import annotations

import copy
import json
import logging
from typing import Any

from certified_turtles.agents.json_agent_protocol import (
    build_protocol_system_message,
    extract_user_visible_assistant_text,
    message_text_content,
    parse_agent_response,
    patch_completion_assistant_markdown,
    tool_outputs_user_message,
)
from certified_turtles.agents.registry import get_subagent
from certified_turtles.mws_gpt.client import MWSGPTClient
from certified_turtles.tools.parent_tools import get_parent_tools, parse_agent_tool_name
from certified_turtles.tools.registry import openai_tools_for_names, run_primitive_tool

logger = logging.getLogger(__name__)


def _tool_names_from_openai_tools(tool_list: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for t in tool_list:
        if t.get("type") != "function":
            continue
        fn = t.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return names


def _inject_json_protocol_system(
    messages: list[dict[str, Any]],
    *,
    tool_list: list[dict[str, Any]],
) -> None:
    """Вставляет системное сообщение с единым JSON-протоколом и каталогом тулов (первым в списке)."""
    if not tool_list:
        return
    messages.insert(0, {"role": "system", "content": build_protocol_system_message(tool_list)})


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
    inner_messages: list[dict[str, Any]] = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]
    inner_tools = openai_tools_for_names(spec.tool_names)
    inner = run_agent_chat(
        client,
        model,
        inner_messages,
        tools=inner_tools,
        max_tool_rounds=spec.max_inner_rounds,
        delegate_depth=delegate_depth + 1,
        max_delegate_depth=max_delegate_depth,
        **chat_kwargs,
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
            **chat_kwargs,
        )
    return run_primitive_tool(name, arguments)


def run_agent_chat(
    client: MWSGPTClient,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = "auto",
    max_tool_rounds: int = 10,
    delegate_depth: int = 0,
    max_delegate_depth: int = 3,
    **chat_kwargs: Any,
) -> dict[str, Any]:
    """
    Цикл оркестратора: при ненулевом списке тулов — единый JSON-протокол в ответах assistant
    (см. `json_agent_protocol`) и исполнение `calls`; иначе — одиночный chat без протокола.
    """
    work = copy.deepcopy(messages)
    tool_list = get_parent_tools() if tools is None else tools
    use_json_protocol = bool(tool_list)
    if use_json_protocol:
        _inject_json_protocol_system(work, tool_list=tool_list)
    allowed = _tool_names_from_openai_tools(tool_list) if tool_list else set()
    last_raw: dict[str, Any] | None = None
    rounds = 0

    while rounds < max_tool_rounds:
        rounds += 1
        call_kwargs = {k: v for k, v in chat_kwargs.items() if k not in ("tools", "tool_choice")}
        if not use_json_protocol:
            if tools is not None and tool_choice is not None:
                call_kwargs["tool_choice"] = tool_choice
            if tools is not None:
                call_kwargs["tools"] = tools
        logger.debug("agent chat round %s messages=%s depth=%s json_proto=%s", rounds, len(work), delegate_depth, use_json_protocol)
        last_raw = client.chat_completions(model, work, **call_kwargs)
        choice = _first_choice(last_raw)
        msg = _choice_message(choice)
        raw_text = message_text_content(msg)

        if not use_json_protocol:
            work.append(copy.deepcopy(msg))
            return {
                "messages": work,
                "completion": last_raw,
                "tool_rounds_used": rounds,
                "truncated": False,
            }

        parsed = parse_agent_response(raw_text)
        if parsed is None:
            logger.warning("agent JSON protocol: parse failed at round %s", rounds)
            work.append({"role": "assistant", "content": raw_text})
            patched = patch_completion_assistant_markdown(last_raw, raw_text)
            return {
                "messages": work,
                "completion": patched,
                "tool_rounds_used": rounds,
                "truncated": False,
            }

        work.append({"role": "assistant", "content": raw_text})
        calls = parsed["calls"]
        if not calls:
            md = parsed["assistant_markdown"]
            patched = patch_completion_assistant_markdown(last_raw, md)
            return {
                "messages": work,
                "completion": patched,
                "tool_rounds_used": rounds,
                "truncated": False,
            }

        outputs: list[str] = []
        for c in calls:
            name = c["name"]
            args = c["arguments"]
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
                **chat_kwargs,
            )
            outputs.append(out)
        work.append({"role": "user", "content": tool_outputs_user_message(calls, outputs)})

    visible = ""
    if last_raw is not None:
        try:
            lm = _choice_message(_first_choice(last_raw))
            visible = extract_user_visible_assistant_text(message_text_content(lm))
        except ValueError:
            visible = ""
    tail = "\n\n[лимит раундов агента: ответ может быть неполным.]" if not visible.strip() else ""
    patched = patch_completion_assistant_markdown(last_raw or {}, (visible or "") + tail)
    return {
        "messages": work,
        "completion": patched,
        "tool_rounds_used": rounds,
        "truncated": True,
    }
