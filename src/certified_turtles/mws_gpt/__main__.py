from __future__ import annotations

import argparse
import json
import os
import sys

from certified_turtles.services.llm import LLMService

from .client import DEFAULT_BASE_URL, MWSGPTClient, MWSGPTError


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _parse_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key:
                out[key] = val
    return out


def _find_project_root(start: str | None = None) -> str | None:
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, "pyproject.toml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _load_env_files() -> None:
    """Подтягивает .env: корень репо (рядом с pyproject.toml), затем cwd; cwd перекрывает файл из корня. Не затирает переменные из shell."""
    merged: dict[str, str] = {}
    root = _find_project_root()
    if root:
        merged.update(_parse_env_file(os.path.join(root, ".env")))
    merged.update(_parse_env_file(os.path.join(os.getcwd(), ".env")))
    for key, val in merged.items():
        if key not in os.environ:
            os.environ[key] = val


def cmd_models(_args: argparse.Namespace, client: MWSGPTClient) -> int:
    _print_json(client.list_models())
    return 0


def cmd_chat(args: argparse.Namespace, client: MWSGPTClient) -> int:
    messages: list[dict[str, str]] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.message})
    extra = {}
    if args.temperature is not None:
        extra["temperature"] = args.temperature
    if args.max_tokens is not None:
        extra["max_tokens"] = args.max_tokens
    _print_json(client.chat_completions(args.model, messages, **extra))
    return 0


def cmd_complete(args: argparse.Namespace, client: MWSGPTClient) -> int:
    extra = {}
    if args.temperature is not None:
        extra["temperature"] = args.temperature
    if args.max_tokens is not None:
        extra["max_tokens"] = args.max_tokens
    _print_json(client.completions(args.model, args.prompt, **extra))
    return 0


def cmd_embed(args: argparse.Namespace, client: MWSGPTClient) -> int:
    _print_json(client.embeddings(args.model, args.input_text))
    return 0


def cmd_agent(args: argparse.Namespace, client: MWSGPTClient) -> int:
    messages: list[dict] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.prompt})
    extra: dict = {}
    if args.temperature is not None:
        extra["temperature"] = args.temperature
    if args.max_tokens is not None:
        extra["max_tokens"] = args.max_tokens
    service = LLMService(client)
    out = service.run_agent(
        args.model,
        messages,
        max_agent_tokens=getattr(args, "max_agent_tokens", None),
        **extra,
    )
    _print_json(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env_files()

    parser = argparse.ArgumentParser(
        description="MWS GPT API: модели, чат, completion, embeddings (см. MWS-GPT.md)",
        epilog=(
            "Модель должна быть из allowlist вашего API-ключа. Пример чата: "
            '%(prog)s chat --model mws-gpt-alpha -m "Привет" --system "Ты помощник"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MWS_API_BASE", DEFAULT_BASE_URL),
        help=f"Базовый URL (по умолчанию {DEFAULT_BASE_URL} или MWS_API_BASE)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Ключ (иначе MWS_API_KEY / MWS_GPT_API_KEY или строка из .env)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_models = sub.add_parser("models", help="GET /v1/models")
    p_models.set_defaults(func=cmd_models)

    p_chat = sub.add_parser("chat", help="POST /v1/chat/completions")
    p_chat.add_argument("--model", required=True)
    p_chat.add_argument("--message", "-m", required=True, help="Сообщение пользователя")
    p_chat.add_argument("--system", "-s", default=None, help="Системный промпт")
    p_chat.add_argument("--temperature", type=float, default=None)
    p_chat.add_argument("--max-tokens", type=int, default=None)
    p_chat.set_defaults(func=cmd_chat)

    p_comp = sub.add_parser("complete", help="POST /v1/completions")
    p_comp.add_argument("--model", required=True)
    p_comp.add_argument("--prompt", "-p", required=True)
    p_comp.add_argument("--temperature", type=float, default=None)
    p_comp.add_argument("--max-tokens", type=int, default=None)
    p_comp.set_defaults(func=cmd_complete)

    p_emb = sub.add_parser("embed", help="POST /v1/embeddings")
    p_emb.add_argument("--model", required=True)
    p_emb.add_argument("--input", "-i", dest="input_text", required=True)
    p_emb.set_defaults(func=cmd_embed)

    p_agent = sub.add_parser(
        "agent",
        help="Агент: chat/completions с tools (web_search) и циклом до финального ответа",
    )
    p_agent.add_argument("--model", required=True)
    p_agent.add_argument(
        "--prompt",
        "-p",
        required=True,
        help="Запрос пользователя (одно сообщение user)",
    )
    p_agent.add_argument("--system", "-s", default=None, help="Системный промпт")
    p_agent.add_argument("--temperature", type=float, default=None)
    p_agent.add_argument("--max-tokens", type=int, default=None)
    p_agent.add_argument(
        "--max-agent-tokens",
        type=int,
        default=int(os.environ.get("CT_MAX_AGENT_TOKENS", "128000")),
        help="Token budget for the agent loop",
    )
    p_agent.set_defaults(func=cmd_agent)

    ns = parser.parse_args(argv)
    try:
        client = MWSGPTClient(api_key=ns.api_key, base_url=ns.base_url)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        return int(ns.func(ns, client))
    except MWSGPTError as e:
        print(f"Ошибка API: {e}", file=sys.stderr)
        if e.body:
            print(e.body, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
