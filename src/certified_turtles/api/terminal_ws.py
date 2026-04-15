"""
WebSocket ↔ PTY bridge.

Two modes:
  /ws/terminal           — new shell (default bash)
  /ws/terminal?session=X — attach to shared tmux session X (creates if needed)

xterm.js in browser connects here and gets a real terminal.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import termios

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

router = APIRouter()

_SESSION_NAME = "gpthub"


def _tmux_bin() -> str | None:
    return shutil.which("tmux")


def _ensure_tmux_session(name: str):
    """Create tmux session if it doesn't exist."""
    tmux = _tmux_bin()
    if not tmux:
        return
    r = subprocess.run([tmux, "has-session", "-t", name], capture_output=True)
    if r.returncode != 0:
        subprocess.run([tmux, "new-session", "-d", "-s", name, "-x", "120", "-y", "40"])


@router.websocket("/ws/terminal")
async def terminal_ws(
    ws: WebSocket,
    session: str | None = Query(default=None),
):
    await ws.accept()

    tmux = _tmux_bin()

    if session and tmux:
        # Attach to shared tmux session
        _ensure_tmux_session(session)
        cmd = [tmux, "attach-session", "-t", session]
    else:
        cmd = [os.environ.get("SHELL", "/bin/bash")]

    # Fork PTY
    pid, fd = pty.fork()

    if pid == 0:
        # Child
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env.setdefault("ANTHROPIC_BASE_URL", "http://localhost:8000/anthropic")
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        # Load .env
        for candidate in [".env", os.path.join(os.path.expanduser("~"), "Projects/certified turtles/.env")]:
            if os.path.isfile(candidate):
                with open(candidate) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, _, v = line.partition("=")
                            env.setdefault(k.strip(), v.strip())
                break
        if env.get("MWS_API_KEY") and not env.get("ANTHROPIC_API_KEY"):
            env["ANTHROPIC_API_KEY"] = env["MWS_API_KEY"]
        os.execvpe(cmd[0], cmd, env)

    # Parent — bridge
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    _set_winsize(fd, 40, 120)

    async def _read_pty():
        try:
            while True:
                await asyncio.sleep(0.008)
                try:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    await ws.send_text(data.decode("utf-8", errors="replace"))
                except BlockingIOError:
                    continue
                except OSError:
                    break
        except Exception:
            pass

    async def _write_pty():
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if "text" in msg:
                    os.write(fd, msg["text"].encode("utf-8"))
                elif "bytes" in msg:
                    raw = msg["bytes"]
                    if raw and raw[0:1] == b"r" and len(raw) == 5:
                        cols = int.from_bytes(raw[1:3], "big")
                        rows = int.from_bytes(raw[3:5], "big")
                        _set_winsize(fd, rows, cols)
                    else:
                        os.write(fd, raw)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    try:
        await asyncio.gather(_read_pty(), _write_pty())
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass


def _set_winsize(fd: int, rows: int, cols: int):
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
