from __future__ import annotations

import copy
import json
import logging
from typing import Any

from certified_turtles.agents.registry import get_subagent
from certified_turtles.agents.tool_call_recovery import recover_tool_calls_from_assistant_message
from certified_turtles.mws_gpt.client import MWSGPTClient
from certified_turtles.tools.parent_tools import get_parent_tools, parse_agent_tool_name
from certified_turtles.tools.registry import openai_tools_for_names, run_primitive_tool

logger = logging.getLogger(__name__)

_TOOL_POLICY = (
    "[Инструменты] Если нужны факты из сети, содержимое страницы по URL, список моделей MWS или выполнение кода на сервере — "
    "используй **вызов функции** в ответе API (`tool_calls`). Markdown-блок ```python ... ``` сам по себе **не исполняется** на сервере; "
    "для реального вывода нужен соответствующий тул. "
    "**Не вставляй** в текст ответа JSON с полем `tool_calls` — сервер не исполняет такие блоки, только нативное поле ответа модели."
)

_MWS_MODELS_HINT = (
    "[Модели MWS] Актуальный список id моделей для ключа сервера — только через инструмент `mws_list_models`, "
    "а не через сетевые импорты внутри `execute_python`."
)

_EXECUTE_PYTHON_GUIDANCE = (
    "[Исполнение кода] В каталоге тулов есть `execute_python`: передай полный скрипт в аргументе `code` — "
    "он выполнится на сервере (numpy, matplotlib, pandas и др. из белого списка), в ответе будет stdout/stderr и ссылки на файлы. "
    "Для расчётов, проверки логики, симуляций и графиков **вызывай этот инструмент**, а не ограничивайся только markdown-блоком с кодом, "
    "если нужен реальный вывод. Графики сохраняй, например: "
    "`plt.savefig(os.path.join(CT_RUN_OUTPUT_DIR, \"plot.png\"))`."
)


def _tool_names_from_openai_tools(tool_list: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for t in tool_list:
        if t.get("type") != "function":
            continue
        fn = t.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return names


def _inject_tool_guidance(
    messages: list[dict[str, Any]],
    *,
    tool_list: list[dict[str, Any]],
) -> None:
    """Системные подсказки: общая политика tool_calls + узкие хинты по доступным тулам."""
    if not tool_list:
        return
    names = _tool_names_from_openai_tools(tool_list)
    parts: list[str] = [_TOOL_POLICY]
    if "mws_list_models" in names:
        parts.append(_MWS_MODELS_HINT)
    if "execute_python" in names:
        parts.append(_EXECUTE_PYTHON_GUIDANCE)
    hint = "\n\n".join(parts)
    for m in messages:
        if m.get("role") != "system":
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = c.rstrip() + "\n\n" + hint
        elif isinstance(c, list):
            merged = False
            for part in reversed(c):
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("text", "input_text"):
                    key = "text" if "text" in part else "input_text"
                    part[key] = str(part.get(key) or "").rstrip() + "\n\n" + hint
                    merged = True
                    break
            if not merged:
                c.append({"type": "text", "text": hint})
        else:
            m["content"] = (str(c).rstrip() + "\n\n" + hint) if c is not None else hint
        return
    messages.insert(0, {"role": "system", "content": hint})


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
                final_text = str(c)
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
    Цикл оркестратора: chat/completions с тулами из `get_parent_tools()` (примитивы + `agent_{id}`),
    исполнение tool_calls, повтор до финала или лимита раундов.
    """
    work = copy.deepcopy(messages)
    tool_list = get_parent_tools() if tools is None else tools
    if tool_list:
        _inject_tool_guidance(work, tool_list=tool_list)
    last_raw: dict[str, Any] | None = None
    rounds = 0

    while rounds < max_tool_rounds:
        rounds += 1
        call_kwargs = dict(chat_kwargs)
        if tool_list:
            call_kwargs["tools"] = tool_list
            if tool_choice is not None:
                call_kwargs["tool_choice"] = tool_choice
        logger.debug("agent chat round %s messages=%s depth=%s", rounds, len(work), delegate_depth)
        last_raw = client.chat_completions(model, work, **call_kwargs)
        choice = _first_choice(last_raw)
        msg = _choice_message(choice)
        msg_out = copy.deepcopy(msg)
        tcalls = list(msg_out.get("tool_calls") or [])
        allowed = _tool_names_from_openai_tools(tool_list) if tool_list else set()
        if not tcalls and allowed:
            recovered, new_content = recover_tool_calls_from_assistant_message(msg_out, allowed)
            if recovered:
                msg_out["tool_calls"] = recovered
                tcalls = recovered
                if new_content is not None:
                    msg_out["content"] = new_content or None
        work.append(msg_out)

        if not tcalls:
            return {
                "messages": work,
                "completion": last_raw,
                "tool_rounds_used": rounds,
                "truncated": False,
            }

        for tc in tcalls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args: Any = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {"_raw_arguments": (raw_args or "")[:2000]}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            if not isinstance(args, dict):
                args = {}
            tid = str(tc.get("id") or "")
            content = _execute_tool_call(
                name,
                args,
                client=client,
                model=model,
                delegate_depth=delegate_depth,
                max_delegate_depth=max_delegate_depth,
                **chat_kwargs,
            )
            work.append({"role": "tool", "tool_call_id": tid, "content": content})

    return {
        "messages": work,
        "completion": last_raw,
        "tool_rounds_used": rounds,
        "truncated": True,
    }
