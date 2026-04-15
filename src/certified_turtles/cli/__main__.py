"""
GPTHub Code — AI coding assistant in your terminal.

Usage:
    uv run python -m certified_turtles.cli
    uv run python -m certified_turtles.cli "explain this project"
    uv run python -m certified_turtles.cli --model gpt-oss-120b
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.text import Text

from certified_turtles.cli.chat import ChatSession

console = Console()

BANNER = """[bold cyan]
   _____ _____ _____ _   _       _        ___          _
  / ____|  __ \\_   _| | | |     | |      / __|___   __| | ___
 | |  __| |__) || | | |_| |_   _| |__   | |  / _ \\ / _` |/ _ \\
 | | |_ |  ___/ | | |  _  | | | | '_ \\  | |_| (_) | (_| |  __/
 |  __  | |    _| |_| | | | |_| | |_) |  \\___\\___/ \\__,_|\\___|
  \\___| |_|   |_____|_| |_|\\__,_|_.__/
[/bold cyan]
[dim]AI coding assistant powered by MWS GPT. Type /help for commands.[/dim]
"""

DEFAULT_API_BASE = "https://api.gpt.mws.ru"
DEFAULT_MODEL = "gpt-oss-120b"


def _load_config() -> tuple[str, str, str]:
    """Load API config from .env / env vars / CLI args."""
    load_dotenv()

    api_base = os.environ.get("MWS_API_BASE", DEFAULT_API_BASE)
    api_key = os.environ.get("MWS_API_KEY", "")
    model = DEFAULT_MODEL

    # Parse CLI args
    args = sys.argv[1:]
    i = 0
    initial_prompt = []
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif args[i] == "--api-base" and i + 1 < len(args):
            api_base = args[i + 1]
            i += 2
        elif args[i] == "--api-key" and i + 1 < len(args):
            api_key = args[i + 1]
            i += 2
        elif args[i] in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            initial_prompt.append(args[i])
            i += 1

    if not api_key:
        console.print("[red]No API key. Set MWS_API_KEY in .env or pass --api-key[/red]")
        sys.exit(1)

    return api_base, api_key, model


def _handle_slash(cmd: str, session: ChatSession) -> bool:
    """Handle slash commands. Returns True if handled."""
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()

    if command in ("/exit", "/quit", "/q"):
        console.print("[dim]Bye![/dim]")
        sys.exit(0)

    if command == "/help":
        console.print(
            "[bold]Commands:[/bold]\n"
            "  /help          — this message\n"
            "  /clear         — reset conversation\n"
            "  /compact       — trim old messages\n"
            "  /model <name>  — switch model\n"
            "  /exit          — quit\n"
            "\n[bold]Shortcuts:[/bold]\n"
            "  Ctrl+C         — cancel current generation\n"
            "  Ctrl+D         — exit\n"
        )
        return True

    if command == "/clear":
        session._init_system()
        console.print("[dim]Conversation cleared.[/dim]")
        return True

    if command == "/compact":
        session.compact()
        return True

    if command == "/model":
        if len(parts) > 1:
            session.model = parts[1]
            console.print(f"[dim]Model → {session.model}[/dim]")
        else:
            console.print(f"[dim]Current model: {session.model}[/dim]")
        return True

    return False


def main():
    api_base, api_key, model = _load_config()

    console.print(BANNER)
    console.print(f"[dim]Model: {model} | API: {api_base} | cwd: {os.getcwd()}[/dim]\n")

    session = ChatSession(api_base, api_key, model)

    # One-shot mode: run prompt from CLI args and exit
    args = sys.argv[1:]
    non_flag_args = []
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args) and args[i] != "--help":
            i += 2
        else:
            non_flag_args.append(args[i])
            i += 1
    initial_prompt = " ".join(non_flag_args).strip()

    if initial_prompt:
        session.send(initial_prompt)
        return

    # Interactive REPL
    prompt_session = PromptSession(history=InMemoryHistory())

    while True:
        try:
            user_input = prompt_session.prompt("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye![/dim]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            if _handle_slash(user_input, session):
                continue

        try:
            session.send(user_input)
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted.[/dim]")


if __name__ == "__main__":
    main()
