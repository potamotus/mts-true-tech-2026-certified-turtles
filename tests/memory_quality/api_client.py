"""GPTHub test client for memory quality evaluation.

Sends messages to the local API and polls the memory directory
for extracted memories.  Supports both local and Docker-based APIs.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from certified_turtles.memory_runtime.storage import (
    memory_dir,
    scope_slug,
)

_log = logging.getLogger(__name__)

DEFAULT_EXTRACTION_TIMEOUT = 150
POLL_INTERVAL = 1.0
_CONTAINER_CLAUDE_HOME = "/tmp/certified_turtles_claude_like"


def _detect_docker_container() -> str | None:
    """Return the API container name/id if running via docker compose, else None."""
    try:
        out = subprocess.check_output(
            ["docker", "compose", "ps", "-q", "api"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        if out:
            return out.splitlines()[0]
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return None


class GPTHubTestClient:
    """Client that sends messages to the API and observes memory side-effects."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        claude_home: str | None = None,
        docker_container: str | None = "auto",
    ):
        self.base_url = base_url.rstrip("/")
        self.claude_home = claude_home or os.environ.get(
            "CT_CLAUDE_HOME", _CONTAINER_CLAUDE_HOME
        )
        self._http = httpx.Client(base_url=self.base_url, timeout=120)

        # Resolve Docker container
        if docker_container == "auto":
            self._container = _detect_docker_container()
        else:
            self._container = docker_container

        if self._container:
            _log.info("Docker mode: reading files from container %s", self._container)
        else:
            _log.info("Local mode: reading files from host filesystem at %s", self.claude_home)

    @property
    def is_docker(self) -> bool:
        return self._container is not None

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """Return True if the API is reachable."""
        try:
            r = self._http.get("/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def list_models(self) -> list[str]:
        """GET /v1/models and return model ids."""
        r = self._http.get("/v1/models")
        r.raise_for_status()
        data = r.json().get("data", [])
        return [m["id"] for m in data]

    def send_message(
        self,
        session_id: str,
        scope_id: str,
        messages: list[dict[str, str]],
        model: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/chat/completions with ct_session_id and ct_scope_id."""
        body: dict[str, Any] = {
            "messages": messages,
            "ct_session_id": session_id,
            "ct_scope_id": scope_id,
            "use_agent": False,  # plain mode — we only care about memory extraction side-effect
        }
        if model:
            body["model"] = model
        _log.info("Sending message scope=%s session=%s", scope_id, session_id)
        r = self._http.post("/v1/chat/completions", json=body)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Memory directory observation — Docker or local
    # ------------------------------------------------------------------

    def _scope_memory_path(self, scope_id: str) -> str:
        """Compute the memory directory path string (works for both local and Docker)."""
        slug = scope_slug(scope_id)
        return f"{_CONTAINER_CLAUDE_HOME}/projects/{slug}/memory"

    def _docker_exec(self, cmd: list[str]) -> str:
        """Run a command inside the Docker container and return stdout."""
        full_cmd = ["docker", "exec", self._container, *cmd]
        try:
            return subprocess.check_output(
                full_cmd, stderr=subprocess.DEVNULL, timeout=10
            ).decode("utf-8", errors="replace")
        except subprocess.SubprocessError:
            return ""

    def _list_memory_files_docker(self, scope_id: str) -> list[str]:
        """List .md file paths (relative to memory dir) inside the container.

        Searches recursively since the extractor may create subdirectories
        like memory/user/foo.md.
        """
        mem_path = self._scope_memory_path(scope_id)
        out = self._docker_exec(["find", mem_path, "-name", "*.md", "-type", "f"])
        results = []
        for line in out.strip().splitlines():
            if not line.strip():
                continue
            # Get path relative to memory dir
            rel = line.replace(mem_path + "/", "", 1) if line.startswith(mem_path) else line
            name = rel.rsplit("/", 1)[-1] if "/" in rel else rel
            if name.startswith(".") or name == "MEMORY.md":
                continue
            # Store the full relative path (e.g. "user/foo.md" or "foo.md")
            results.append(rel)
        return sorted(results)

    def _read_file_docker(self, scope_id: str, rel_path: str) -> str:
        """Read a single file from inside the container (rel_path relative to memory dir)."""
        mem_path = self._scope_memory_path(scope_id)
        return self._docker_exec(["cat", f"{mem_path}/{rel_path}"])

    def _local_memory_dir(self, scope_id: str) -> Path:
        """Compute the memory directory path on the host."""
        old_env = os.environ.get("CT_CLAUDE_HOME")
        os.environ["CT_CLAUDE_HOME"] = self.claude_home
        try:
            return memory_dir(scope_id)
        finally:
            if old_env is None:
                os.environ.pop("CT_CLAUDE_HOME", None)
            else:
                os.environ["CT_CLAUDE_HOME"] = old_env

    def _list_memory_files_local(self, scope_id: str) -> list[str]:
        """List .md file paths (relative to memory dir) on the host."""
        d = self._local_memory_dir(scope_id)
        if not d.exists():
            return []
        results = []
        for p in d.rglob("*.md"):
            if p.name.startswith(".") or p.name == "MEMORY.md":
                continue
            rel = str(p.relative_to(d))
            results.append(rel)
        return sorted(results)

    def _read_file_local(self, scope_id: str, rel_path: str) -> str:
        """Read a single file from the host filesystem."""
        d = self._local_memory_dir(scope_id)
        p = d / rel_path
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # Unified interface
    # ------------------------------------------------------------------

    def list_memory_files(self, scope_id: str) -> list[str]:
        """List memory filenames for a scope (Docker-aware)."""
        if self.is_docker:
            return self._list_memory_files_docker(scope_id)
        return self._list_memory_files_local(scope_id)

    def read_file(self, scope_id: str, filename: str) -> str:
        """Read a memory file's content (Docker-aware)."""
        if self.is_docker:
            return self._read_file_docker(scope_id, filename)
        return self._read_file_local(scope_id, filename)

    def read_scope_memories(self, scope_id: str) -> list[dict[str, Any]]:
        """Read all memory files for a scope, returning filename + content."""
        results = []
        for name in self.list_memory_files(scope_id):
            content = self.read_file(scope_id, name)
            results.append({"filename": name, "content": content})
        return results

    def wait_for_extraction(
        self,
        scope_id: str,
        before_files: list[str],
        timeout: float = DEFAULT_EXTRACTION_TIMEOUT,
    ) -> list[dict[str, Any]]:
        """Poll memory dir until new files appear or timeout.

        Returns list of NEW memory files (those not in before_files).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current_names = set(self.list_memory_files(scope_id))
            new_names = current_names - set(before_files)
            if new_names:
                _log.info("New memories detected: %s", new_names)
                return [
                    {"filename": name, "content": self.read_file(scope_id, name)}
                    for name in sorted(new_names)
                ]
            time.sleep(POLL_INTERVAL)
        _log.info("Extraction timeout (%ss) — no new files", timeout)
        return []

    def cleanup_scope(self, scope_id: str) -> None:
        """Remove the scope's memory directory for test isolation."""
        if self.is_docker:
            mem_path = self._scope_memory_path(scope_id)
            self._docker_exec(["rm", "-rf", mem_path])
        else:
            import shutil
            d = self._local_memory_dir(scope_id)
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            parent = d.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @staticmethod
    def unique_ids(scenario_id: str) -> tuple[str, str]:
        """Generate unique session_id and scope_id for a scenario."""
        suffix = uuid.uuid4().hex[:8]
        scope_id = f"mqtest-{scenario_id}-{suffix}"
        session_id = f"mqsess-{scenario_id}-{suffix}"
        return session_id, scope_id
