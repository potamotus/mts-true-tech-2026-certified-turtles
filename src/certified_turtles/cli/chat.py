"""Chat loop with streaming + tool calling via MWS GPT."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from certified_turtles.cli.tools import TOOL_DEFS, run_tool

console = Console()

MAX_TOOL_ROUNDS = 25

SYSTEM_PROMPT = """You are GPTHub Code — an AI coding assistant running in the user's terminal.
You have direct access to their filesystem and can run commands.

Working directory: {cwd}

Available tools: read_file, edit_file, write_file, bash, glob_search, grep.

Guidelines:
- Read files before editing them.
- Use glob_search/grep to find files before guessing paths.
- Run tests/linters after making changes.
- Be concise. Show what you did, not what you plan to do.
- When editing, use edit_file with exact string matching. For new files use write_file.
- For bash: prefer specific commands over broad ones. Don't run destructive commands without the user asking.
"""


def _get_git_context() -> str:
    """Quick git context for the system prompt."""
    parts = []
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if branch.returncode == 0:
            parts.append(f"Git branch: {branch.stdout.strip()}")
    except Exception:
        pass
    return "\n".join(parts)


class ChatSession:
    def __init__(self, api_base: str, api_key: str, model: str):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.messages: list[dict[str, Any]] = []
        self._init_system()

    def _init_system(self):
        ctx = _get_git_context()
        system = SYSTEM_PROMPT.format(cwd=os.getcwd())
        if ctx:
            system += "\n" + ctx
        self.messages = [{"role": "system", "content": system}]

    def _stream_completion(self, stream: bool = True) -> dict | None:
        """Call MWS GPT and stream response tokens to terminal."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        body = {
            "model": self.model,
            "messages": self.messages,
            "tools": TOOL_DEFS,
            "stream": stream,
            "temperature": 0.2,
        }

        if not stream:
            with httpx.Client(timeout=120) as client:
                resp = client.post(f"{self.api_base}/v1/chat/completions", json=body, headers=headers)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]

        # Streaming
        full_content = ""
        tool_calls_accum: dict[int, dict] = {}
        started_text = False

        with httpx.Client(timeout=120) as client:
            with client.stream("POST", f"{self.api_base}/v1/chat/completions", json=body, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    delta = chunk.get("choices", [{}])[0].get("delta", {})

                    # Text content
                    token = delta.get("content")
                    if token:
                        if not started_text:
                            console.print()  # blank line before response
                            started_text = True
                        console.print(token, end="", highlight=False)
                        full_content += token

                    # Tool calls (accumulated across chunks)
                    for tc in delta.get("tool_calls", []):
                        idx = tc["index"]
                        if idx not in tool_calls_accum:
                            tool_calls_accum[idx] = {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.get("id"):
                            tool_calls_accum[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            tool_calls_accum[idx]["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_calls_accum[idx]["function"]["arguments"] += fn["arguments"]

        if started_text:
            console.print()  # newline after streamed text

        # Build assistant message
        msg: dict[str, Any] = {"role": "assistant"}
        if full_content:
            msg["content"] = full_content
        if tool_calls_accum:
            msg["tool_calls"] = [tool_calls_accum[i] for i in sorted(tool_calls_accum)]
        else:
            msg["tool_calls"] = None

        return msg

    def _execute_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """Execute tools and return tool result messages."""
        results = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}

            # Show what we're doing
            args_preview = ", ".join(f"{k}={repr(v)[:60]}" for k, v in args.items())
            console.print(
                Text(f"  ⚡ {name}({args_preview})", style="dim cyan"),
            )

            output = run_tool(name, args)

            # Show truncated result
            preview = output[:200].replace("\n", "↵ ")
            if len(output) > 200:
                preview += "..."
            console.print(Text(f"  → {preview}", style="dim"))

            results.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": output,
            })

        return results

    def send(self, user_input: str):
        """Send user message and handle response + tool loop."""
        self.messages.append({"role": "user", "content": user_input})

        for _round in range(MAX_TOOL_ROUNDS):
            try:
                assistant_msg = self._stream_completion(stream=True)
            except httpx.HTTPStatusError as e:
                console.print(f"[red]API error: {e.response.status_code} — {e.response.text[:200]}[/red]")
                return
            except httpx.ConnectError:
                console.print("[red]Connection error. Is the API running?[/red]")
                return

            if not assistant_msg:
                return

            self.messages.append(assistant_msg)

            tool_calls = assistant_msg.get("tool_calls")
            if not tool_calls:
                break

            tool_results = self._execute_tool_calls(tool_calls)
            self.messages.extend(tool_results)

    def compact(self):
        """Remove old messages to stay within context limits."""
        if len(self.messages) > 60:
            system = self.messages[0]
            recent = self.messages[-40:]
            self.messages = [system] + recent
            console.print("[dim]Context compacted.[/dim]")
