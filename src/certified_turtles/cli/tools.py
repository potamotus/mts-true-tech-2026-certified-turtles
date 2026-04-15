"""CLI tools: file operations, bash, search."""

from __future__ import annotations

import glob as _glob
import os
import subprocess
from pathlib import Path

# ── OpenAI tool definitions ──

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file. Returns its content with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (absolute or relative to cwd)"},
                    "offset": {"type": "integer", "description": "Start line (0-based). Optional."},
                    "limit": {"type": "integer", "description": "Max lines to read. Optional."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact string in a file. old_string must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "old_string": {"type": "string", "description": "Exact text to find and replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "File content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command and return stdout+stderr. Use for git, npm, build, test, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts').",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "path": {"type": "string", "description": "Base directory (default: cwd)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regex pattern. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "File or directory to search in (default: cwd)"},
                    "include": {"type": "string", "description": "Glob filter for files (e.g. '*.py')"},
                },
                "required": ["pattern"],
            },
        },
    },
]


# ── Tool handlers ──

def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def read_file(path: str, offset: int = 0, limit: int = 200) -> str:
    p = _resolve(path)
    if not p.is_file():
        return f"Error: {p} not found"
    lines = p.read_text(errors="replace").splitlines()
    end = min(offset + limit, len(lines))
    numbered = [f"{i + 1}\t{lines[i]}" for i in range(offset, end)]
    header = f"({len(lines)} lines total)" if end < len(lines) else ""
    return "\n".join(numbered) + ("\n" + header if header else "")


def edit_file(path: str, old_string: str, new_string: str) -> str:
    p = _resolve(path)
    if not p.is_file():
        return f"Error: {p} not found"
    text = p.read_text(errors="replace")
    count = text.count(old_string)
    if count == 0:
        return "Error: old_string not found in file"
    if count > 1:
        return f"Error: old_string found {count} times, must be unique"
    p.write_text(text.replace(old_string, new_string, 1))
    return "OK — file updated"


def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"OK — wrote {len(content)} bytes to {p}"


def bash(command: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=os.getcwd(),
        )
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            out = f"[exit {r.returncode}]\n{out}"
        return out[:8000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


def glob_search(pattern: str, path: str | None = None) -> str:
    base = _resolve(path) if path else Path.cwd()
    matches = sorted(_glob.glob(str(base / pattern), recursive=True))
    if not matches:
        return "No matches"
    # Relative paths for readability
    rel = []
    for m in matches[:100]:
        try:
            rel.append(str(Path(m).relative_to(base)))
        except ValueError:
            rel.append(m)
    result = "\n".join(rel)
    if len(matches) > 100:
        result += f"\n... and {len(matches) - 100} more"
    return result


def grep(pattern: str, path: str | None = None, include: str | None = None) -> str:
    base = path or "."
    cmd = ["grep", "-rn", "--color=never", "-E", pattern]
    if include:
        cmd += ["--include", include]
    cmd.append(base)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=os.getcwd())
        out = r.stdout.strip()
        lines = out.splitlines()
        if len(lines) > 50:
            return "\n".join(lines[:50]) + f"\n... ({len(lines)} total matches)"
        return out if out else "No matches"
    except Exception as e:
        return f"Error: {e}"


# ── Dispatcher ──

_HANDLERS = {
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "bash": bash,
    "glob_search": glob_search,
    "grep": grep,
}


def run_tool(name: str, args: dict) -> str:
    handler = _HANDLERS.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return handler(**args)
    except Exception as e:
        return f"Error running {name}: {e}"
